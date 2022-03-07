# This file is part of BenchExec, a framework for reliable benchmarking:
# https://github.com/sosy-lab/benchexec
#
# SPDX-FileCopyrightText: 2007-2020 Dirk Beyer <https://www.sosy-lab.org>
#
# SPDX-License-Identifier: Apache-2.0

import logging
import os
import pathlib
import random
import signal
import string
import tempfile
import time


from benchexec import util, BenchExecException
from benchexec.cgroups import Cgroups


uid = os.getuid()
CGROUP_NAME_PREFIX = "benchmark_"


def _find_cgroup_mount():
    """
    Return the mountpoint of the cgroupv2 unified hierarchy.
    @return Path mountpoint
    """
    try:
        with open("/proc/mounts", "rt") as mountsFile:
            for mount in mountsFile:
                mount = mount.split(" ")
                if mount[2] == "cgroup2":
                    return pathlib.Path(mount[1])
    except OSError:
        logging.exception("Cannot read /proc/mounts")


def _find_own_cgroups():
    """
    For all subsystems, return the information in which (sub-)cgroup this process is in.
    (Each process is in exactly cgroup in each hierarchy.)
    @return a generator of tuples (subsystem, cgroup)
    """
    try:
        with open("/proc/self/cgroup", "rt") as ownCgroupsFile:
            return _parse_proc_pid_cgroup(ownCgroupsFile)
    except OSError:
        logging.exception("Cannot read /proc/self/cgroup")


def _parse_proc_pid_cgroup(cgroup_file):
    """
    Parse a /proc/*/cgroup file into tuples of (subsystem,cgroup).
    @param content: An iterable over the lines of the file.
    @return: a generator of tuples
    """
    mountpoint = _find_cgroup_mount()
    for line in cgroup_file:
        own_cgroup = line.strip().split(":")[2][1:]
        path = mountpoint / own_cgroup

    return path


def kill_all_tasks_in_cgroup(cgroup, ensure_empty=True):
    tasksFile = cgroup / "cgroup.procs"

    i = 0
    while True:
        i += 1
        # TODO We can probably remove this loop over signals and just send
        # SIGKILL. We added this loop when killing sub-processes was not reliable
        # and we did not know why, but now it is reliable.
        for sig in [signal.SIGKILL, signal.SIGINT, signal.SIGTERM]:
            with open(tasksFile, "rt") as tasks:
                task = None
                for task in tasks:
                    task = task.strip()
                    if i > 1:
                        logging.warning(
                            "Run has left-over process with pid %s "
                            "in cgroup %s, sending signal %s (try %s).",
                            task,
                            cgroup,
                            sig,
                            i,
                        )
                    util.kill_process(int(task), sig)

                if task is None or not ensure_empty:
                    return  # No process was hanging, exit
            # wait for the process to exit, this might take some time
            time.sleep(i * 0.5)


class CgroupsV2(Cgroups):
    def __init__(self, subsystems=None, cgroup_procinfo=None, fallback=True):
        self.version = 2

        self.IO = "io"
        self.CPU = "cpu"
        self.CPUSET = "cpuset"
        self.MEMORY = "memory"
        self.PID = "pids"
        self.FREEZE = "freeze"
        self.KILL = "kill"

        super(CgroupsV2, self).__init__(subsystems, cgroup_procinfo, fallback)

        self.path = (
            next(iter(self.subsystems.values())) if len(self.subsystems) else None
        )

    @property
    def known_subsystems(self):
        return {
            # cgroups for BenchExec
            self.IO,
            self.CPU,
            self.CPUSET,
            self.MEMORY,
            self.PID,
            # not really a subsystem anymore, but implicitly supported
            self.FREEZE,
            self.KILL,
        }

    def _supported_subsystems(self, cgroup_procinfo=None, fallback=True):
        logging.debug(
            "Analyzing /proc/mounts and /proc/self/cgroup to determine cgroups."
        )
        cgroup_path = _find_own_cgroups()

        with open(cgroup_path / "cgroup.controllers") as subsystems_file:
            subsystems = set(subsystems_file.readline().strip().split())

        # introduced in 5.14
        if (cgroup_path / "cgroup.kill").exists():
            subsystems.add(self.KILL)

        # always supported in v2
        subsystems.add(self.FREEZE)

        # basic support always available in v2, this supports everything we use
        subsystems.add(self.CPU)

        return {k: cgroup_path for k in subsystems}

    def create_fresh_child_cgroup(self, subsystems, move_to_child=False):
        """
        Create child cgroups of the current cgroup for at least the given subsystems.
        @return: A Cgroup instance representing the new child cgroup(s).
        """
        subsystems = set(subsystems)
        assert subsystems.issubset(self.subsystems.keys())

        tasks = set(util.read_file(self.path / "cgroup.procs").split())
        if tasks and not move_to_child:
            raise BenchExecException(
                "Cannot create cgroups v2 child on non-empty parent without moving tasks"
            )

        allowed_pids = {str(p) for p in util.get_pgrp_pids(os.getpgid(0))}
        if len(tasks) > 1 and not tasks <= allowed_pids and move_to_child:
            raise BenchExecException(
                "runexec must be the only running process in its cgroup. Either install pystemd "
                "for benchexec to handle this itself, prefix the command with `systemd-run --user --scope -p Delegate=yes` "
                "or otherwise prepare the cgroup hierarchy to make sure of this and the subtree being "
                "writable by the executing user."
            )

        prefix = "runexec_main_" if move_to_child else CGROUP_NAME_PREFIX
        child_path = pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=self.path))

        if move_to_child and tasks:
            prev_delegated_controllers = set(
                util.read_file(self.path / "cgroup.subtree_control").split()
            )
            for c in prev_delegated_controllers:
                util.write_file(f"-{c}", self.path / "cgroup.subtree_control")

            for t in tasks:
                try:
                    util.write_file(t, child_path / "cgroup.procs")
                except OSError as e:
                    logging.warn(f"Could not move pid {t} to {child_path}: {e}")

            for c in prev_delegated_controllers:
                util.write_file(f"+{c}", self.path / "cgroup.subtree_control")

        controllers = set(util.read_file(self.path / "cgroup.controllers").split())
        controllers_to_delegate = controllers & subsystems

        for c in controllers_to_delegate:
            util.write_file(f"+{c}", self.path / "cgroup.subtree_control")

        # basic cpu controller support without being enabled
        child_subsystems = controllers_to_delegate | {"cpu"}
        return CgroupsV2({c: child_path for c in child_subsystems})

    def move_to_scope(self):
        logging.debug("Moving runexec main process to scope")

        pids = util.get_pgrp_pids(os.getpgid(0))
        try:
            from pystemd.dbuslib import DBus
            from pystemd.dbusexc import DBusFileNotFoundError
            from pystemd.systemd1 import Manager, Unit

            with DBus(user_mode=True) as bus, Manager(bus=bus) as manager:
                unit_params = {
                    # workaround for not declared parameters, remove in the future
                    b"_custom": (b"PIDs", b"au", [int(p) for p in pids]),
                    b"Delegate": True,
                }

                random_suffix = "".join(
                    random.choices(string.ascii_letters + string.digits, k=8)
                )
                name = f"benchexec_{random_suffix}.scope".encode()
                manager.Manager.StartTransientUnit(name, b"fail", unit_params)

                with Unit(name, bus=bus):
                    self.subsystems = self._supported_subsystems()
                    self.paths = set(self.subsystems.values())
                    self.path = next(iter(self.subsystems.values()))
                    logging.debug(
                        f"moved to scope {name}, subsystems: {self.subsystems}"
                    )
        except ImportError:
            logging.warn("pystemd could not be imported")
        except DBusFileNotFoundError:
            logging.warn("no user DBus found, not using pystemd")

        self.create_fresh_child_cgroup(self.subsystems.keys(), move_to_child=True)

    def add_task(self, pid):
        """
        Add a process to the cgroups represented by this instance.
        """
        with open(self.path / "cgroup.procs", "w") as tasksFile:
            tasksFile.write(str(pid))

    def get_all_tasks(self, subsystem):
        """
        Return a generator of all PIDs currently in this cgroup for the given subsystem.
        """
        with open(self.path / "cgroup.procs") as tasksFile:
            for line in tasksFile:
                yield int(line)

    def kill_all_tasks(self):
        """
        Kill all tasks in this cgroup and all its children cgroups forcefully.
        Additionally, the children cgroups will be deleted.
        """

        def kill_all_tasks_in_cgroup_recursively(cgroup, delete):
            for dirpath, dirs, _files in os.walk(cgroup, topdown=False):
                for subCgroup in dirs:
                    subCgroup = pathlib.Path(dirpath) / subCgroup
                    kill_all_tasks_in_cgroup(subCgroup, ensure_empty=delete)

                    if delete:
                        self._remove_cgroup(subCgroup)

            kill_all_tasks_in_cgroup(cgroup, ensure_empty=delete)

        if self.KILL in self.subsystems:
            util.write_file("1", self.path / "cgroup.kill")
            return

        # First, we go through all cgroups recursively while they are frozen and kill
        # all processes. This helps against fork bombs and prevents processes from
        # creating new subgroups while we are trying to kill everything.
        # All processes will stay until they are thawed (so we cannot check for cgroup
        # emptiness and we cannot delete subgroups).
        freezer_file = self.path / "cgroup.freeze"

        util.write_file("1", freezer_file)
        kill_all_tasks_in_cgroup_recursively(self.path, delete=False)
        util.write_file("0", freezer_file)

        # Second, we go through all cgroups again, kill what is left,
        # check for emptiness, and remove subgroups.
        kill_all_tasks_in_cgroup_recursively(self.path, delete=True)

    def read_cputime(self):
        """
        Read the cputime usage of this cgroup. CPU cgroup needs to be available.
        @return cputime usage in seconds
        """
        cpu_stats = dict(self.get_key_value_pairs(self.CPU, "stat"))

        return float(cpu_stats["usage_usec"]) / 1_000_000

    def read_max_mem_usage(self):
        logging.debug("Memory-usage not supported in cgroups v2")

        return

    def read_mem_pressure(self):
        mem_stats = dict(self.get_key_value_pairs(self.MEMORY, "pressure"))
        mem_some_stats = mem_stats["some"].split(" ")
        stats_map = {s[0]: s[1] for s in (s.split("=") for s in mem_some_stats)}

        return float(stats_map["total"]) / 1_000_000

    def read_cpu_pressure(self):
        cpu_stats = dict(self.get_key_value_pairs(self.CPU, "pressure"))
        cpu_some_stats = cpu_stats["some"].split(" ")
        stats_map = {s[0]: s[1] for s in (s.split("=") for s in cpu_some_stats)}

        return float(stats_map["total"]) / 1_000_000

    def read_io_pressure(self):
        io_stats = dict(self.get_key_value_pairs(self.IO, "pressure"))
        io_some_stats = io_stats["some"].split(" ")
        stats_map = {s[0]: s[1] for s in (s.split("=") for s in io_some_stats)}

        return float(stats_map["total"]) / 1_000_000

    def read_usage_per_cpu(self):
        logging.debug("Usage per CPU not supported in cgroups v2")

        return {}

    def read_available_cpus(self):
        return util.parse_int_list(self.get_value(self.CPUSET, "cpus.effective"))

    def read_available_mems(self):
        return util.parse_int_list(self.get_value(self.CPUSET, "mems.effective"))

    def read_io_stat(self):
        bytes_read = 0
        bytes_written = 0
        for io_line in self.get_file_lines(self.IO, "stat"):
            dev_no, *stats = io_line.split(" ")
            stats_map = {s[0]: s[1] for s in (s.split("=") for s in stats)}
            bytes_read += int(stats_map["rbytes"])
            bytes_written += int(stats_map["wbytes"])
        return bytes_read, bytes_written

    def has_tasks(self, path):
        return os.path.getsize(path / "cgroup.procs") > 0

    def write_memory_limit(self, limit):
        self.set_value(self.MEMORY, "max", limit)

    def read_memory_limit(self):
        return int(self.get_value(self.MEMORY, "max"))

    def disable_swap(self):
        self.set_value(self.MEMORY, "swap.max", "0")

    def read_oom_count(self):
        for line in self.get_file_lines(self.MEMORY, "events"):
            k, v = line.split(" ")
            if k == "oom_kill":
                return int(v)

        return 0