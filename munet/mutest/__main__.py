# -*- coding: utf-8 eval: (blacken-mode 1) -*-
#
# December 2 2022, Christian Hopps <chopps@labn.net>
#
# Copyright (c) 2022, LabN Consulting, L.L.C.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; see the file COPYING; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA
#
"""Command to execute mutests."""

import asyncio
import logging
import os
import subprocess
import sys
import time

from argparse import ArgumentParser
from argparse import Namespace
from copy import deepcopy
from pathlib import Path
from typing import Union

from munet import parser
from munet.base import Bridge
from munet.mutest import userapi as uapi
from munet.native import L3Node
from munet.native import Munet
from munet.parser import async_build_topology
from munet.parser import get_config


# We want all but critical to fit in 5 characters for alignment
logging.addLevelName(logging.WARNING, "WARN")
root_logger = logging.getLogger("")
exec_formatter = logging.Formatter("%(asctime)s %(levelname)5s: %(name)s: %(message)s")


async def get_unet(config: dict, rundir: Path, unshare: bool = True):
    """Create and run a new Munet topology.

    The topology is built from the given ``config`` to run inside the path indicated
    by ``rundir``. If ``unshare`` is True then the process will unshare into it's
    own private namespace.

    Args:
        config: a config dictionary obtained from ``munet.parser.get_config``. This
          value will be modified and stored in the built ``Munet`` object.
        rundir: the path to the run directory for this topology.
        unshare: True to unshare the process into it's own private namespace.

    Yields:
        Munet: The constructed and running topology.
    """
    try:
        unet = await async_build_topology(
            config, rundir=str(rundir), unshare_inline=unshare
        )
    except Exception as error:
        logging.debug("unet build failed: %s", error, exc_info=True)
        raise

    try:
        tasks = await unet.run()
    except Exception as error:
        logging.debug("unet run failed: %s", error, exc_info=True)
        await unet.async_delete()
        raise

    logging.debug("unet topology running")

    # Pytest is supposed to always return even if exceptions
    try:
        yield unet
    except Exception as error:
        logging.error("unet fixture: yield unet unexpected exception: %s", error)

    await unet.async_delete()

    # No one ever awaits these so cancel them
    logging.debug("unet fixture: cleanup")
    for task in tasks:
        task.cancel()

    # Reset the class variables so auto number is predictable
    logging.debug("unet fixture: resetting ords to 1")
    L3Node.next_ord = 1
    Bridge.next_ord = 1


def common_root(path1: Union[str, Path], path2: Union[str, Path]) -> Path:
    """Find the common root between 2 paths.

    Args:
        path1: Path
        path2: Path
    Returns:
        Path: the shared root components between ``path1`` and ``path2``.

    Examples:
        >>> common_root("/foo/bar/baz", "/foo/bar/zip/zap")
        PosixPath('/foo/bar')
        >>> common_root("/foo/bar/baz", "/fod/bar/zip/zap")
        PosixPath('/')
    """
    apath1 = Path(path1).absolute().parts
    apath2 = Path(path2).absolute().parts
    alen = min(len(apath1), len(apath2))
    common = None
    for a, b in zip(apath1[:alen], apath2[:alen]):
        if a != b:
            break
        common = common.joinpath(a) if common else Path(a)
    return common


async def collect(args: Namespace):
    """Collect test files.

    Files must match the pattern ``mutest_*.py``, and their containing
    directory must have a munet config file present. This function also changes
    the current directory to the common parent of all the tests, and paths are
    returned relative to the common directory.

    Args:
      args: argparse results

    Returns:
      (commondir, tests, configs): where ``commondir`` is the path representing
        the common parent directory of all the testsd, ``tests`` is a
        dictionary of lists of test files, keyed on their containing directory
        path, and ``configs`` is a dictionary of config dictionaries also keyed
        on its containing directory path. The directory paths are relative to a
        common ancestor.
    """
    file_select = args.file_select
    upaths = args.paths if args.paths else ["."]
    globpaths = set()
    for upath in (Path(x) for x in upaths):
        if upath.is_file():
            paths = {upath.absolute()}
        else:
            paths = {x.absolute() for x in Path(upath).rglob(file_select)}
        globpaths |= paths
    tests = {}
    configs = {}

    # Find the common root
    # We don't actually need this anymore, the idea was prefix test names
    # with uncommon paths elements to automatically differentiate them.
    common = None
    sortedpaths = []
    for path in sorted(globpaths):
        sortedpaths.append(path)
        dirpath = path.parent
        common = common_root(common, dirpath) if common else dirpath

    ocwd = Path().absolute()
    try:
        os.chdir(common)
        # Work with relative paths to the common directory
        for path in (x.relative_to(common) for x in sortedpaths):
            dirpath = path.parent
            if dirpath not in configs:
                try:
                    configs[dirpath] = get_config(search=[dirpath])
                except FileNotFoundError:
                    logging.warning(
                        "Skipping '%s' as munet.{yaml,toml,json} not found in '%s'",
                        path,
                        dirpath,
                    )
                    continue
            if dirpath not in tests:
                tests[dirpath] = []
            tests[dirpath].append(path.absolute())
    finally:
        os.chdir(ocwd)
    return common, tests, configs


async def execute_test(
    unet: Munet,
    test: Path,
    args: Namespace,
    test_num: int,
    exec_handler: logging.Handler,
) -> (int, int, int, Exception):
    """Execute a test case script.

    Using the built and running topology in ``unet`` for targets
    execute the test case script file ``test``.

    Args:
        unet: a running topology.
        test: path to the test case script file.
        args: argparse results.
        test_num: the number of this test case in the run.
        exec_handler: exec file handler to add to test loggers which do not propagate.
    """
    test_name = testname_from_path(test)
    script = open(f"{test}", "r", encoding="utf-8").read()

    # Get test case loggers
    logger = logging.getLogger(f"mutest.output.{test_name}")
    reslog = logging.getLogger(f"mutest.results.{test_name}")
    logger.addHandler(exec_handler)
    reslog.addHandler(exec_handler)

    # reslog.info("-" * 70)
    reslog.info("START: %s:%s from %s", test_num, test_name, test.stem)

    targets = dict(unet.hosts.items())
    targets["."] = unet
    tc = uapi.TestCase(test_num, test_name, test, targets, logger, reslog)

    # pylint: disable=possibly-unused-variable,exec-used
    step = tc.step
    step_json = tc.step_json
    match_step = tc.match_step
    match_step_json = tc.match_step_json
    wait_step = tc.wait_step
    wait_step_json = tc.wait_step_json
    include = tc.include

    start_time = time.time()
    e = None
    try:
        s2 = f"async def _{test_name}():\n " + script.replace("\n", "\n ") + "\n"
        exec(s2)
        await locals()[f"_{test_name}"]()
    except Exception as error:
        logging.error("Unexpected exception during test %s: %s", test_name, error)
        e = error
    finally:
        passed, failed = tc.end_test()

    run_time = time.time() - start_time
    status = "PASS" if not failed else "FAIL"
    reslog.info(
        "stats: %s:%s:  %d pass, %d fail, %4.2fs elapsed",
        test_num,
        test_name,
        passed,
        failed,
        run_time,
    )
    reslog.info("-" * 70)
    reslog.info("END: %s %s:%s\n", status, test_num, test_name)

    return passed, failed, e


def testname_from_path(path: Path) -> str:
    """Return test name based on the path to the test file.

    Args:
       path: path to the test file.

    Returns:
       str: the name of the test.
    """
    return str(Path(path).stem).replace("/", ".")


async def run_tests(args):
    reslog = logging.getLogger("mutest.results")
    sheader = "=== RUN START "
    reslog.info(sheader + sheader[0] * (70 - len(sheader)) + "\n")

    _, tests, configs = await collect(args)
    results = []
    errlog = logging.getLogger("mutest.error")
    tnum = 0
    start_time = time.time()
    for dirpath in tests:
        test_files = tests[dirpath]
        for test in test_files:
            tnum += 1
            config = deepcopy(configs[dirpath])
            test_name = testname_from_path(test)
            rundir = args.rundir.joinpath(test_name)

            # Add an test case exec file handler to the root logger and result logger
            exec_path = rundir.joinpath("mutest-exec.log")
            exec_path.parent.mkdir(parents=True, exist_ok=True)
            exec_handler = logging.FileHandler(exec_path, "w")
            exec_handler.setFormatter(exec_formatter)
            root_logger.addHandler(exec_handler)

            try:
                async for unet in get_unet(config, rundir):
                    passed, failed, e = await execute_test(
                        unet, test, args, tnum, exec_handler
                    )
            except Exception as error:
                errlog.error("Error executing test %s: %s", test, error, exc_info=True)
                passed, failed, e = 0, 0, error

            # Remove the test case exec file handler form the root logger.
            root_logger.removeHandler(exec_handler)

            results.append((test_name, passed, failed, e))
    run_time = time.time() - start_time

    sheader = "--- RUN SUMMARY "
    reslog.info(sheader + sheader[0] * (70 - len(sheader)))

    tnum = 0
    tpassed = 0
    tfailed = 0
    texc = 0

    snum = 0
    spassed = 0
    sfailed = 0
    for result in results:
        test_name, passed, failed, e = result
        tnum += 1
        spassed += passed
        sfailed += failed
        if e:
            texc += 1
        if failed or e:
            s = "FAIL"
            tfailed += 1
        else:
            s = "PASS"
            tpassed += 1

        reslog.info(" %s  %s:%s", s, tnum, test_name)

    reslog.info(
        "run stats: %s steps, %s pass, %s fail, %s abort, %4.2fs elapsed",
        spassed + sfailed,
        spassed,
        sfailed,
        texc,
        run_time,
    )
    reslog.info("-" * 70)
    reslog.info("END RUN: %s tests, %s pass, %s fail", tnum, tpassed, tfailed)

    return 1 if tfailed else 0


async def async_main(args):
    status = 3
    try:
        # For some reson we are not catching exceptions raised inside
        status = await run_tests(args)
    except KeyboardInterrupt:
        logging.info("Exiting (async_main), received KeyboardInterrupt in main")
    except Exception as error:
        logging.info(
            "Exiting (async_main), unexpected exception %s", error, exc_info=True
        )
    logging.debug("async_main returns %s", status)
    return status


def main():
    ap = ArgumentParser()
    ap.add_argument(
        "--dist",
        type=int,
        nargs="?",
        const=-1,
        default=0,
        action="store",
        metavar="NUM-THREADS",
        help="Run in parallel, value is num. of threads or no value for auto",
    )
    ap.add_argument("-d", "--rundir", help="runtime directory for tempfiles, logs, etc")
    ap.add_argument(
        "--file-select", default="mutest_*.py", help="shell glob for finding tests"
    )
    ap.add_argument("--log-config", help="logging config file (yaml, toml, json, ...)")
    ap.add_argument(
        "-v", dest="verbose", action="count", default=0, help="More -v's, more verbose"
    )
    ap.add_argument("paths", nargs="*", help="Paths to collect tests from")
    args = ap.parse_args()

    rundir = args.rundir if args.rundir else "/tmp/mutest"
    args.rundir = Path(rundir)
    os.environ["MUNET_RUNDIR"] = rundir
    subprocess.run(f"mkdir -p {rundir} && chmod 755 {rundir}", check=True, shell=True)

    config = parser.setup_logging(args, config_base="logconf-mutest")
    # Grab the exec formatter from the logging config
    if fconfig := config.get("formatters", {}).get("exec"):
        global exec_formatter  # pylint: disable=W291
        exec_formatter = logging.Formatter(
            fconfig.get("format"), fconfig.get("datefmt")
        )

    status = 4
    try:
        status = asyncio.run(async_main(args))
    except KeyboardInterrupt:
        logging.info("Exiting (main), received KeyboardInterrupt in main")
    except Exception as error:
        logging.info("Exiting (main), unexpected exception %s", error, exc_info=True)

    sys.exit(status)


if __name__ == "__main__":
    main()