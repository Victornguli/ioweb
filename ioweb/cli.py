from pprint import pprint
import sys
import re
import time
import os.path
import json
import logging
from argparse import ArgumentParser
from importlib import import_module
from collections import defaultdict
from threading import Thread, Event
from subprocess import Popen, PIPE, STDOUT, TimeoutExpired

from ioweb.stat import Stat

from pythonjsonlogger import jsonlogger

from .crawler import Crawler

logger = logging.getLogger('crawler.cli')


def find_crawlers_in_module(mod, reg):
    for key in dir(mod):
        val = getattr(mod, key)
        if (
                isinstance(val, type)
                and issubclass(val, Crawler)
                and val is not Crawler
            ):
            logger.error(
                'Found crawler %s in module %s',
                val.__name__, mod.__file__
            )
            reg[val.__name__] = val


def collect_crawlers():
    reg = {}

    # Give crawlers in current directory max priority
    # Otherwise `/web/crawler/crawlers` packages are imported
    # when crawler is installed with `pip -e /web/crawler`
    sys.path.insert(0, os.getcwd())

    for location in ('crawlers',):
        try:
            mod = import_module(location)
        except ImportError as ex:
            #if path not in str(ex):
            logger.exception('Failed to import %s', location)
        else:
            if getattr(mod, '__file__', '').endswith('__init__.py'):
                dir_ = os.path.split(mod.__file__)[0]
                for fname in os.listdir(dir_):
                    if (
                        fname.endswith('.py')
                        and not fname.endswith('__init__.py')
                    ):
                        target_mod = '%s.%s' % (location, fname[:-3])
                        try:
                            mod = import_module(target_mod)
                        except ImportError as ex:
                            #if path not in str(ex):
                            logger.exception('Failed to import %s', target_mod)
                        else:
                            find_crawlers_in_module(mod, reg)
            else:
                find_crawlers_in_module(mod, reg)

    return reg


def setup_logging(logging_format='text', network_logs=False):#, control_logs=False):
    assert logging_format in ('text', 'json')
    #logging.basicConfig(level=logging.DEBUG)
    logging.getLogger('urllib3.connectionpool').setLevel(level=logging.ERROR)
    logging.getLogger('urllib3.util.retry').setLevel(level=logging.ERROR)
    logging.getLogger('urllib3.poolmanager').setLevel(level=logging.ERROR)
    logging.getLogger('ioweb.urllib3_custom').setLevel(level=logging.ERROR)
    if not network_logs:
        logging.getLogger('ioweb.network_service').propagate = False
    #if not control_logs:
    #    logging.getLogger('crawler.control').propagate = False

    hdl = logging.StreamHandler()
    logger = logging.getLogger()
    logger.addHandler(hdl)
    logger.setLevel(logging.DEBUG)
    if logging_format == 'json':
        #for hdl in logging.getLogger().handlers:
        hdl.setFormatter(jsonlogger.JsonFormatter())


def format_elapsed_time(total_sec):
    hours = minutes = 0
    if total_sec > 3600:
        hours, total_sec = divmod(total_sec, 3600)
    if total_sec > 60:
        minutes, total_sec = divmod(total_sec, 60)
    return '%02d:%02d:%.2f' % (hours, minutes, total_sec)


def get_crawler(crawler_id):
    reg = collect_crawlers()
    if crawler_id not in reg:
        sys.stderr.write(
            'Could not find %s crawler\n' % crawler_id
        )
        sys.exit(1)
    else:
        return reg[crawler_id]


def run_subcommand_crawl(opts):
    setup_logging(
        logging_format=opts.logging_format,
        network_logs=opts.network_logs
    )#, control_logs=opts.control_logs)
    cls = get_crawler(opts.crawler_id)
    extra_data = {}
    for key in cls.extra_cli_args():
        opt_key = 'extra_%s' % key.replace('-', '_')
        extra_data[key] = getattr(opts, opt_key)
    bot = cls(
        network_threads=opts.network_threads,
        extra_data=extra_data,
        debug=opts.debug,
        stat_logging=(opts.stat_logging == 'yes'),
        stat_logging_format=opts.stat_logging_format,
    )
    try:
        if opts.profile:
            import cProfile
            import pyprof2calltree
            import pstats

            profile_file = 'var/%s.prof' % opts.crawler_id
            profile_tree_file = 'var/%s.prof.out' % opts.crawler_id

            prof = cProfile.Profile()
            try:
                prof.runctx('bot.run()', globals(), locals())
            finally:
                stats = pstats.Stats(prof)
                stats.strip_dirs()
                pyprof2calltree.convert(stats, profile_tree_file)
        else:
            bot.run()
    except KeyboardInterrupt:
        bot.fatal_error_happened.set()
    print('Stats:')
    for key, val in sorted(bot.stat.total_counters.items()):
        print(' * %s: %s' % (key, val))
    if bot._run_started:
        print('Elapsed: %s' % format_elapsed_time(time.time() - bot._run_started))
    else:
        print('Elapsed: NA')
    if bot.fatal_error_happened.is_set():
        sys.exit(1)
    else:
        sys.exit(0)


def run_subcommand_foo(opts):
    print('COMMAND FOO')


def thread_worker(crawler_cls, threads, stat, preg, evt_error, evt_init):
    counters = defaultdict(int)
    try:
        cmd = [
            'ioweb',
            'crawl',
            crawler_cls,
            '-t%d' % threads,
            '--logging-format=json',
            '--stat-logging-format=json'
        ]
        proc = Popen(cmd, stdout=PIPE, stderr=STDOUT)
        preg[proc.pid] = proc
    finally:
        evt_init.set()
    while True:
        line = proc.stdout.readline()
        try:
            obj = json.loads(line.decode('ascii'))
        except (ValueError, UnicodeDecodeError):
            uline = line.decode('utf-8', errors='replace')
            logging.error(
                '[pid=%d] RAW-MSG: %s',
                proc.pid, uline.rstrip()
            )
        else:
            msg = obj['message']
            if 'exc_info' in obj:
                msg += obj['exc_info']
            try:
                msg_obj = json.loads(msg)
            except ValueError:
                logging.error('[pid=%d] TEXT-MSG: %s', proc.pid, msg)
            else:
                if 'eps' in msg_obj and 'counter' in msg_obj:
                    for key in msg_obj['eps'].keys():
                        stat.speed_keys = set(stat.speed_keys) | set([key])
                    for key, val in msg_obj['counter'].items():
                        delta = val - counters[key]
                        counters[key] = val
                        stat.inc(key, delta)
                else:
                    logging.error('[pid=%d] JSON-MSG: %s', proc.pid, msg_obj)
        ret = proc.poll()
        if ret is not None:
            if ret !=0:
                evt_error.set()
            break


def run_subcommand_multi(opts):
    setup_logging(
        logging_format='text',
        network_logs=True,
    )
    stat = Stat()

    pool = []
    preg = {}
    evt_error = Event()
    try:
        for _ in range(opts.workers):
            evt_init = Event()
            th = Thread(
                target=thread_worker,
                args=[opts.crawler_cls, opts.threads, stat, preg, evt_error, evt_init]
            )
            th.daemon = True
            th.start()
            pool.append(th)
            evt_init.wait()

        evt_stop = Event()
        while (
                not evt_stop.is_set()
                and not evt_error.is_set()
            ):
            num_done = 0
            for proc in preg.values():
                try:
                    proc.wait(timeout=0.1)
                except TimeoutExpired:
                    pass
                else:
                    num_done += 1
                if evt_error.is_set():
                    break
                if num_done == len(pool):
                    evt_stop.set()
                    break
    finally:
        for proc in preg.values():
            print('Finishing process pid=%d' % proc.pid)
            proc.terminate()
            proc.wait()


def command_ioweb():
    parser = ArgumentParser()#add_help=False)

    crawler_cls = None
    if len(sys.argv) > 2:
        if sys.argv[1] == 'crawl':
            crawler_cls = get_crawler(sys.argv[2])

    subparsers = parser.add_subparsers(
        dest='command',
        title='Subcommands of ioweb command',
        description='',
    )

    # Crawl
    crawl_subparser = subparsers.add_parser(
        'crawl', help='Run crawler',
    )
    crawl_subparser.add_argument('crawler_id')
    crawl_subparser.add_argument('-t', '--network-threads', type=int, default=1)
    crawl_subparser.add_argument('-n', '--network-logs', action='store_true', default=False)
    crawl_subparser.add_argument('-p', '--profile', action='store_true', default=False)
    crawl_subparser.add_argument('--debug', action='store_true', default=False)
    crawl_subparser.add_argument(
        '--stat-logging', choices=['yes', 'no'], default='yes',
    )
    crawl_subparser.add_argument(
        '--stat-logging-format', choices=['text', 'json'], default='text',
    )
    crawl_subparser.add_argument(
        '--logging-format', choices=['text', 'json'], default='text',
    )
    #parser.add_argument('--control-logs', action='store_true', default=False)
    if crawler_cls:
        crawler_cls.update_arg_parser(crawl_subparser)

    # Foo
    foo_subparser = subparsers.add_parser(
        'foo', help='Just test subcommand',
    )

    # multi
    multi_subparser = subparsers.add_parser(
        'multi', help='Run multi instances of crawler',
    )
    multi_subparser.add_argument('crawler_cls')
    multi_subparser.add_argument('-w', '--workers', type=int, default=1)
    multi_subparser.add_argument('-t', '--threads', type=int, default=1)

    opts = parser.parse_args()
    if opts.command == 'crawl':
        run_subcommand_crawl(opts)
    elif opts.command == 'foo':
        run_subcommand_foo(opts)
    elif opts.command == 'multi':
        run_subcommand_multi(opts)
    else:
        parser.print_help()
