#!/usr/bin/env python3
"""
Resmoke Test Suite Generator.

Analyze the evergreen history for tests run under the given task and create new evergreen tasks
to attempt to keep the task runtime under a specified amount.
"""
from copy import deepcopy
import datetime
from datetime import timedelta
from inspect import getframeinfo, currentframe
import logging
import math
import os
import re
import sys
from distutils.util import strtobool  # pylint: disable=no-name-in-module
from typing import Dict, List, Set, Sequence, Optional, Any, Match

import click
import requests
import structlog
import yaml

from evergreen.api import EvergreenApi, RetryingEvergreenApi
from evergreen.stats import TestStats

from shrub.v2 import Task, TaskDependency, BuildVariant, ExistingTask, ShrubProject

# Get relative imports to work when the package is not installed on the PYTHONPATH.
if __name__ == "__main__" and __package__ is None:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# pylint: disable=wrong-import-position
import buildscripts.resmokelib.parser as _parser
import buildscripts.resmokelib.suitesconfig as suitesconfig
from buildscripts.util.fileops import write_file_to_dir
import buildscripts.util.read_config as read_config
import buildscripts.util.taskname as taskname
import buildscripts.util.teststats as teststats
from buildscripts.patch_builds.task_generation import TimeoutInfo, resmoke_commands
# pylint: enable=wrong-import-position

LOGGER = structlog.getLogger(__name__)

AVG_SETUP_TIME = int(timedelta(minutes=5).total_seconds())
DEFAULT_TEST_SUITE_DIR = os.path.join("buildscripts", "resmokeconfig", "suites")
CONFIG_FILE = "./.evergreen.yml"
MIN_TIMEOUT_SECONDS = int(timedelta(minutes=5).total_seconds())
MAX_EXPECTED_TIMEOUT = int(timedelta(hours=48).total_seconds())
LOOKBACK_DURATION_DAYS = 14
GEN_SUFFIX = "_gen"

HEADER_TEMPLATE = """# DO NOT EDIT THIS FILE. All manual edits will be lost.
# This file was generated by {file} from
# {suite_file}.
"""

REQUIRED_CONFIG_KEYS = {
    "build_variant",
    "fallback_num_sub_suites",
    "project",
    "task_id",
    "task_name",
}

DEFAULT_CONFIG_VALUES = {
    "generated_config_dir": "generated_resmoke_config",
    "max_tests_per_suite": 100,
    "max_sub_suites": 10,
    "resmoke_args": "",
    "resmoke_repeat_suites": 1,
    "run_multiple_jobs": "true",
    "target_resmoke_time": 60,
    "test_suites_dir": DEFAULT_TEST_SUITE_DIR,
    "use_default_timeouts": False,
    "use_large_distro": False,
}

CONFIG_FORMAT_FN = {
    "fallback_num_sub_suites": int,
    "max_sub_suites": int,
    "max_tests_per_suite": int,
    "target_resmoke_time": int,
}


class ConfigOptions(object):
    """Retrieve configuration from a config file."""

    def __init__(self, config, required_keys=None, defaults=None, formats=None):
        """
        Create an instance of ConfigOptions.

        :param config: Dictionary of configuration to use.
        :param required_keys: Set of keys required by this config.
        :param defaults: Dict of default values for keys.
        :param formats: Dict with functions to format values before returning.
        """
        self.config = config
        self.required_keys = required_keys if required_keys else set()
        self.default_values = defaults if defaults else {}
        self.formats = formats if formats else {}

    @classmethod
    def from_file(cls, filepath, required_keys, defaults, formats):
        """
        Create an instance of ConfigOptions based on the given config file.

        :param filepath: Path to file containing configuration.
        :param required_keys: Set of keys required by this config.
        :param defaults: Dict of default values for keys.
        :param formats: Dict with functions to format values before returning.
        :return: Instance of ConfigOptions.
        """
        return cls(read_config.read_config_file(filepath), required_keys, defaults, formats)

    @property
    def depends_on(self):
        """List of dependencies specified."""
        return split_if_exists(self._lookup(self.config, "depends_on"))

    @property
    def is_patch(self):
        """Is this running in a patch build."""
        patch = self.config.get("is_patch")
        if patch:
            return strtobool(patch)
        return None

    @property
    def repeat_suites(self):
        """How many times should the suite be repeated."""
        return int(self.resmoke_repeat_suites)

    @property
    def suite(self):
        """Return test suite is being run."""
        return self.config.get("suite", self.task)

    @property
    def task(self):
        """Return task being run."""
        return remove_gen_suffix(self.task_name)

    @property
    def run_tests_task(self):
        """Return name of task name for s3 folder containing generated tasks config."""
        return self.task

    @property
    def run_tests_build_variant(self):
        """Return name of build_variant for s3 folder containing generated tasks config."""
        return self.build_variant

    @property
    def run_tests_build_id(self):
        """Return name of build_id for s3 folder containing generated tasks config."""
        return self.build_id

    @property
    def create_misc_suite(self):
        """Whether or not a _misc suite file should be created."""
        return True

    @property
    def display_task_name(self):
        """Return the name to use as the display task."""
        return self.task

    @property
    def gen_task_set(self):
        """Return the set of tasks used to generate this configuration."""
        return {self.task_name}

    @property
    def variant(self):
        """Return build variant is being run on."""
        return self.build_variant

    def _lookup(self, config, item):
        if item not in config:
            if item in self.required_keys:
                raise KeyError(f"{item} must be specified in configuration.")
            return self.default_values.get(item, None)

        if item in self.formats and item in config:
            return self.formats[item](config[item])

        return config.get(item, None)

    def __getattr__(self, item):
        """Determine the value of the given attribute."""
        return self._lookup(self.config, item)

    def __repr__(self):
        """Provide a string representation of this object for debugging."""
        required_values = [f"{key}: {self.config[key]}" for key in REQUIRED_CONFIG_KEYS]
        return f"ConfigOptions({', '.join(required_values)})"


def enable_logging(verbose):
    """Enable verbose logging for execution."""

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="[%(asctime)s - %(name)s - %(levelname)s] %(message)s",
        level=level,
        stream=sys.stdout,
    )
    structlog.configure(logger_factory=structlog.stdlib.LoggerFactory())


def write_file_dict(directory: str, file_dict: Dict[str, str]) -> None:
    """
    Write files in the given dictionary to disk.

    The keys of the dictionary should be the filenames to write and the values should be
    the contents to write to each file.

    If the given directory does not exist, it will be created.

    :param directory: Directory to write files to.
    :param file_dict: Dictionary of files to write.
    """
    for name, contents in file_dict.items():
        write_file_to_dir(directory, name, contents)


def read_yaml(directory: str, filename: str) -> Dict:
    """
    Read the given yaml file.

    :param directory: Directory containing file.
    :param filename: Name of file to read.
    :return: Yaml contents of file.
    """
    with open(os.path.join(directory, filename), "r") as fileh:
        return yaml.safe_load(fileh)


def split_if_exists(str_to_split):
    """Split the given string on "," if it is not None."""
    if str_to_split:
        return str_to_split.split(",")
    return None


def remove_gen_suffix(task_name):
    """Remove '_gen' suffix from task_name."""
    if task_name.endswith(GEN_SUFFIX):
        return task_name[:-4]
    return task_name


def string_contains_any_of_args(string, args):
    """
    Return whether array contains any of a group of args.

    :param string: String being checked.
    :param args: Args being analyzed.
    :return: True if any args are found in the string.
    """
    return any(arg in string for arg in args)


def divide_remaining_tests_among_suites(remaining_tests_runtimes, suites):
    """Divide the list of tests given among the suites given."""
    suite_idx = 0
    for test_file, runtime in remaining_tests_runtimes:
        current_suite = suites[suite_idx]
        current_suite.add_test(test_file, runtime)
        suite_idx += 1
        if suite_idx >= len(suites):
            suite_idx = 0


def _new_suite_needed(current_suite, test_runtime, max_suite_runtime, max_tests_per_suite):
    """
    Check if a new suite should be created for the given suite.

    :param current_suite: Suite currently being added to.
    :param test_runtime: Runtime of test being added.
    :param max_suite_runtime: Max runtime of a single suite.
    :param max_tests_per_suite: Max number of tests in a suite.
    :return: True if a new test suite should be created.
    """
    if current_suite.get_runtime() + test_runtime > max_suite_runtime:
        # Will adding this test put us over the target runtime?
        return True

    if max_tests_per_suite and current_suite.get_test_count() + 1 > max_tests_per_suite:
        # Will adding this test put us over the max number of tests?
        return True

    return False


def divide_tests_into_suites(suite_name, tests_runtimes, max_time_seconds, max_suites=None,
                             max_tests_per_suite=None):
    """
    Divide the given tests into suites.

    Each suite should be able to execute in less than the max time specified. If a single
    test has a runtime greater than `max_time_seconds`, it will be run in a suite on its own.

    If max_suites is reached before assigning all tests to a suite, the remaining tests will be
    divided up among the created suites.

    Note: If `max_suites` is hit, suites may have more tests than `max_tests_per_suite` and may have
    runtimes longer than `max_time_seconds`.

    :param suite_name: Name of suite being split.
    :param tests_runtimes: List of tuples containing test names and test runtimes.
    :param max_time_seconds: Maximum runtime to add to a single bucket.
    :param max_suites: Maximum number of suites to create.
    :param max_tests_per_suite: Maximum number of tests to add to a single suite.
    :return: List of Suite objects representing grouping of tests.
    """
    suites = []
    Suite.reset_current_index()
    current_suite = Suite(suite_name)
    last_test_processed = len(tests_runtimes)
    LOGGER.debug("Determines suites for runtime", max_runtime_seconds=max_time_seconds,
                 max_suites=max_suites, max_tests_per_suite=max_tests_per_suite)
    for idx, (test_file, runtime) in enumerate(tests_runtimes):
        LOGGER.debug("Adding test", test=test_file, test_runtime=runtime)
        if _new_suite_needed(current_suite, runtime, max_time_seconds, max_tests_per_suite):
            LOGGER.debug("Finished suite", suite_runtime=current_suite.get_runtime(),
                         test_runtime=runtime, max_time=max_time_seconds)
            if current_suite.get_test_count() > 0:
                suites.append(current_suite)
                current_suite = Suite(suite_name)
                if max_suites and len(suites) >= max_suites:
                    last_test_processed = idx
                    break

        current_suite.add_test(test_file, runtime)

    if current_suite.get_test_count() > 0:
        suites.append(current_suite)

    if max_suites and last_test_processed < len(tests_runtimes):
        # We must have hit the max suite limit, just randomly add the remaining tests to suites.
        divide_remaining_tests_among_suites(tests_runtimes[last_test_processed:], suites)

    return suites


def update_suite_config(suite_config, roots=None, excludes=None):
    """
    Update suite config based on the roots and excludes passed in.

    :param suite_config: suite_config to update.
    :param roots: new roots to run, or None if roots should not be updated.
    :param excludes: excludes to add, or None if excludes should not be include.
    :return: updated suite_config
    """
    if roots:
        suite_config["selector"]["roots"] = roots

    if excludes:
        # This must be a misc file, if the exclude_files section exists, extend it, otherwise,
        # create it.
        if "exclude_files" in suite_config["selector"] and \
                suite_config["selector"]["exclude_files"]:
            suite_config["selector"]["exclude_files"] += excludes
        else:
            suite_config["selector"]["exclude_files"] = excludes
    else:
        # if excludes was not specified this must not a misc file, so don"t exclude anything.
        if "exclude_files" in suite_config["selector"]:
            del suite_config["selector"]["exclude_files"]

    return suite_config


def generate_resmoke_suite_config(source_config, source_file, roots=None, excludes=None):
    """
    Read and evaluate the yaml suite file.

    Override selector.roots and selector.excludes with the provided values. Write the results to
    target_suite_name.

    :param source_config: Config of suite to base generated config on.
    :param source_file: Filename of source suite.
    :param roots: Roots used to select tests for split suite.
    :param excludes: Tests that should be excluded from split suite.
    """
    suite_config = update_suite_config(deepcopy(source_config), roots, excludes)

    contents = HEADER_TEMPLATE.format(file=__file__, suite_file=source_file)
    contents += yaml.safe_dump(suite_config, default_flow_style=False)
    return contents


def render_suite_files(suites: List, suite_name: str, test_list: List[str], suite_dir,
                       create_misc_suite: bool) -> Dict:
    """
    Render the given list of suites.

    This will create a dictionary of all the resmoke config files to create with the
    filename of each file as the key and the contents as the value.

    :param suites: List of suites to render.
    :param suite_name: Base name of suites.
    :param test_list: List of tests used in suites.
    :param suite_dir: Directory containing test suite configurations.
    :param create_misc_suite: Whether or not a _misc suite file should be created.
    :return: Dictionary of rendered resmoke config files.
    """
    source_config = read_yaml(suite_dir, suite_name + ".yml")
    suite_configs = {
        f"{os.path.basename(suite.name)}.yml": suite.generate_resmoke_config(source_config)
        for suite in suites
    }
    if create_misc_suite:
        suite_configs[f"{os.path.basename(suite_name)}_misc.yml"] = generate_resmoke_suite_config(
            source_config, suite_name, excludes=test_list)
    return suite_configs


def calculate_timeout(avg_runtime, scaling_factor):
    """
    Determine how long a runtime to set based on average runtime and a scaling factor.

    :param avg_runtime: Average runtime of previous runs.
    :param scaling_factor: scaling factor for timeout.
    :return: timeout to use (in seconds).
    """

    def round_to_minute(runtime):
        """Round the given seconds up to the nearest minute."""
        distance_to_min = 60 - (runtime % 60)
        return int(math.ceil(runtime + distance_to_min))

    return max(MIN_TIMEOUT_SECONDS, round_to_minute(avg_runtime)) * scaling_factor + AVG_SETUP_TIME


def should_tasks_be_generated(evg_api, task_id):
    """
    Determine if we should attempt to generate tasks.

    If an evergreen task that calls 'generate.tasks' is restarted, the 'generate.tasks' command
    will no-op. So, if we are in that state, we should avoid generating new configuration files
    that will just be confusing to the user (since that would not be used).

    :param evg_api: Evergreen API object.
    :param task_id: Id of the task being run.
    :return: Boolean of whether to generate tasks.
    """
    task = evg_api.task_by_id(task_id, fetch_all_executions=True)
    # If any previous execution was successful, do not generate more tasks.
    for i in range(task.execution):
        task_execution = task.get_execution(i)
        if task_execution.is_success():
            return False

    return True


class Suite(object):
    """A suite of tests that can be run by evergreen."""

    _current_index = 0

    def __init__(self, source_name: str) -> None:
        """
        Initialize the object.

        :param source_name: Base name of suite.
        """
        self.tests = []
        self.total_runtime = 0
        self.max_runtime = 0
        self.tests_with_runtime_info = 0
        self.source_name = source_name

        self.index = Suite._current_index
        Suite._current_index += 1

    @classmethod
    def reset_current_index(cls):
        """Reset the current index."""
        Suite._current_index = 0

    def add_test(self, test_file: str, runtime: float):
        """Add the given test to this suite."""

        self.tests.append(test_file)
        self.total_runtime += runtime

        if runtime != 0:
            self.tests_with_runtime_info += 1

        if runtime > self.max_runtime:
            self.max_runtime = runtime

    def should_overwrite_timeout(self):
        """
        Whether the timeout for this suite should be overwritten.

        We should only overwrite the timeout if we have runtime info for all tests.
        """
        return len(self.tests) == self.tests_with_runtime_info

    def get_runtime(self):
        """Get the current average runtime of all the tests currently in this suite."""

        return self.total_runtime

    def get_test_count(self):
        """Get the number of tests currently in this suite."""

        return len(self.tests)

    @property
    def name(self) -> str:
        """Get the name of this suite."""
        return taskname.name_generated_task(self.source_name, self.index, Suite._current_index)

    def generate_resmoke_config(self, source_config: Dict) -> str:
        """
        Generate the contents of resmoke config for this suite.

        :param source_config: Resmoke config to base generate config on.
        :return: Resmoke config to run this suite.
        """
        suite_config = update_suite_config(deepcopy(source_config), roots=self.tests)
        contents = HEADER_TEMPLATE.format(file=__file__, suite_file=self.source_name)
        contents += yaml.safe_dump(suite_config, default_flow_style=False)
        return contents


class EvergreenConfigGenerator(object):
    """Generate evergreen configurations."""

    def __init__(self, suites: List[Suite], options: ConfigOptions, evg_api: EvergreenApi):
        """
        Create new EvergreenConfigGenerator object.

        :param suites: The suite the Evergreen config will be generated for.
        :param options: The ConfigOptions object containing the config file values.
        :param evg_api: Evergreen API object.
        """
        self.suites = suites
        self.options = options
        self.evg_api = evg_api
        self.task_specs = []
        self.task_names = []
        self.build_tasks = None

    def _get_distro(self) -> Optional[Sequence[str]]:
        """Get the distros that the tasks should be run on."""
        if self.options.use_large_distro and self.options.large_distro_name:
            return [self.options.large_distro_name]
        return None

    def _generate_resmoke_args(self, suite_file: str) -> str:
        """
        Generate the resmoke args for the given suite.

        :param suite_file: File containing configuration for test suite.
        :return: arguments to pass to resmoke.
        """
        resmoke_args = (f"--suite={suite_file}.yml --originSuite={self.options.suite} "
                        f" {self.options.resmoke_args}")
        if self.options.repeat_suites and not string_contains_any_of_args(
                resmoke_args, ["repeatSuites", "repeat"]):
            resmoke_args += f" --repeatSuites={self.options.repeat_suites} "

        return resmoke_args

    def _get_run_tests_vars(self, suite_file: str) -> Dict[str, Any]:
        """
        Generate a dictionary of the variables to pass to the task.

        :param suite_file: Suite being run.
        :return: Dictionary containing variables and value to pass to generated task.
        """
        variables = {
            "resmoke_args": self._generate_resmoke_args(suite_file),
            "run_multiple_jobs": self.options.run_multiple_jobs,
            "task": self.options.run_tests_task,
            "build_variant": self.options.run_tests_build_variant,
            "build_id": self.options.run_tests_build_id,
        }

        if self.options.resmoke_jobs_max:
            variables["resmoke_jobs_max"] = self.options.resmoke_jobs_max

        if self.options.use_multiversion:
            variables["task_path_suffix"] = self.options.use_multiversion

        return variables

    def _get_timeout_command(self, max_test_runtime: int, expected_suite_runtime: int,
                             use_default: bool) -> TimeoutInfo:
        """
        Add an evergreen command to override the default timeouts to the list of commands.

        :param max_test_runtime: Maximum runtime of any test in the sub-suite.
        :param expected_suite_runtime: Expected runtime of the entire sub-suite.
        :param use_default: Use default timeouts.
        :return: Timeout information.
        """
        repeat_factor = self.options.repeat_suites
        if (max_test_runtime or expected_suite_runtime) and not use_default:
            timeout = None
            exec_timeout = None
            if max_test_runtime:
                timeout = calculate_timeout(max_test_runtime, 3) * repeat_factor
                LOGGER.debug("Setting timeout", timeout=timeout, max_runtime=max_test_runtime,
                             factor=repeat_factor)
            if expected_suite_runtime:
                exec_timeout = calculate_timeout(expected_suite_runtime, 3) * repeat_factor
                LOGGER.debug("Setting exec_timeout", exec_timeout=exec_timeout,
                             suite_runtime=expected_suite_runtime, factor=repeat_factor)

            if self.options.is_patch and \
                    (timeout > MAX_EXPECTED_TIMEOUT or exec_timeout > MAX_EXPECTED_TIMEOUT):
                frameinfo = getframeinfo(currentframe())
                LOGGER.error(
                    "This task looks like it is expected to run far longer than normal. This is "
                    "likely due to setting the suite 'repeat' value very high. If you are sure "
                    "this is something you want to do, comment this check out in your patch build "
                    "and resubmit", repeat_value=repeat_factor, timeout=timeout,
                    exec_timeout=exec_timeout, code_file=frameinfo.filename,
                    code_line=frameinfo.lineno, max_timeout=MAX_EXPECTED_TIMEOUT)
                raise ValueError("Failing due to expected runtime.")
            return TimeoutInfo.overridden(timeout=timeout, exec_timeout=exec_timeout)

        return TimeoutInfo.default_timeout()

    @staticmethod
    def _is_task_dependency(task: str, possible_dependency: str) -> Optional[Match[str]]:
        """
        Determine if the given possible_dependency belongs to the given task.

        :param task: Name of dependency being checked.
        :param possible_dependency: Task to check if dependency.
        :return: None is task is not a dependency.
        """
        return re.match(f"{task}_(\\d|misc)", possible_dependency)

    def _get_tasks_for_depends_on(self, dependent_task: str) -> List[str]:
        """
        Get a list of tasks that belong to the given dependency.

        :param dependent_task: Dependency to check.
        :return: List of tasks that are a part of the given dependency.
        """
        return [
            str(task.display_name) for task in self.build_tasks
            if self._is_task_dependency(dependent_task, str(task.display_name))
        ]

    def _get_dependencies(self) -> Set[TaskDependency]:
        """Get the set of dependency tasks for these suites."""
        dependencies = {TaskDependency("compile")}
        if not self.options.is_patch:
            # Don"t worry about task dependencies in patch builds, only mainline.
            if self.options.depends_on:
                for dep in self.options.depends_on:
                    depends_on_tasks = self._get_tasks_for_depends_on(dep)
                    for dependency in depends_on_tasks:
                        dependencies.add(TaskDependency(dependency))

        return dependencies

    def _generate_task(self, sub_suite_name: str, sub_task_name: str, target_dir: str,
                       max_test_runtime: Optional[int] = None,
                       expected_suite_runtime: Optional[int] = None) -> Task:
        """
        Generate a shrub evergreen config for a resmoke task.

        :param sub_suite_name: Name of suite being generated.
        :param sub_task_name: Name of task to generate.
        :param target_dir: Directory containing generated suite files.
        :param max_test_runtime: Runtime of the longest test in this sub suite.
        :param expected_suite_runtime: Expected total runtime of this suite.
        :return: Shrub configuration for the described task.
        """
        # pylint: disable=too-many-arguments
        LOGGER.debug("Generating task", sub_suite=sub_suite_name)

        # Evergreen always uses a unix shell, even on Windows, so instead of using os.path.join
        # here, just use the forward slash; otherwise the path separator will be treated as
        # the escape character on Windows.
        target_suite_file = '/'.join([target_dir, os.path.basename(sub_suite_name)])
        run_tests_vars = self._get_run_tests_vars(target_suite_file)

        use_multiversion = self.options.use_multiversion
        timeout_info = self._get_timeout_command(max_test_runtime, expected_suite_runtime,
                                                 self.options.use_default_timeouts)
        commands = resmoke_commands("run generated tests", run_tests_vars, timeout_info,
                                    use_multiversion)

        return Task(sub_task_name, commands, self._get_dependencies())

    def _create_sub_task(self, idx: int, suite: Suite) -> Task:
        """
        Create the sub task for the given suite.

        :param idx: Index of suite to created.
        :param suite: Suite to create.
        :return: Shrub configuration for the suite.
        """
        sub_task_name = taskname.name_generated_task(self.options.task, idx, len(self.suites),
                                                     self.options.variant)
        max_runtime = None
        total_runtime = None
        if suite.should_overwrite_timeout():
            max_runtime = suite.max_runtime
            total_runtime = suite.get_runtime()
        return self._generate_task(suite.name, sub_task_name, self.options.generated_config_dir,
                                   max_runtime, total_runtime)

    def _generate_all_tasks(self) -> Set[Task]:
        """Get a set of shrub task for all the sub tasks."""
        tasks = {self._create_sub_task(idx, suite) for idx, suite in enumerate(self.suites)}

        if self.options.create_misc_suite:
            # Add the misc suite
            misc_suite_name = f"{os.path.basename(self.options.suite)}_misc"
            misc_task_name = f"{self.options.task}_misc_{self.options.variant}"
            tasks.add(
                self._generate_task(misc_suite_name, misc_task_name,
                                    self.options.generated_config_dir))

        return tasks

    def generate_config(self, build_variant: BuildVariant) -> None:
        """
        Generate evergreen configuration.

        :param build_variant: Build variant to add generated configuration to.
        """
        self.build_tasks = self.evg_api.tasks_by_build(self.options.build_id)

        tasks = self._generate_all_tasks()
        generating_task = {ExistingTask(task_name) for task_name in self.options.gen_task_set}
        distros = self._get_distro()
        build_variant.display_task(self.options.display_task_name, execution_tasks=tasks,
                                   execution_existing_tasks=generating_task, distros=distros)


class GenerateSubSuites(object):
    """Orchestrate the execution of generate_resmoke_suites."""

    def __init__(self, evergreen_api: EvergreenApi, config_options: ConfigOptions):
        """
        Initialize the object.

        :param evergreen_api: Evergreen API client.
        :param config_options: Generation configuration options.
        """
        self.evergreen_api = evergreen_api
        self.config_options = config_options
        self.test_list = []

        # Populate config values for methods like list_tests()
        _parser.set_options()

    def calculate_suites(self, start_date: datetime, end_date: datetime) -> List[Suite]:
        """
        Divide tests into suites based on statistics for the provided period.

        :param start_date: Time to start historical analysis.
        :param end_date: Time to end historical analysis.
        :return: List of sub suites to be generated.
        """
        try:
            evg_stats = self.get_evg_stats(self.config_options.project, start_date, end_date,
                                           self.config_options.task, self.config_options.variant)
            if not evg_stats:
                LOGGER.debug("No test history, using fallback suites")
                # This is probably a new suite, since there is no test history, just use the
                # fallback values.
                return self.calculate_fallback_suites()
            target_execution_time_secs = self.config_options.target_resmoke_time * 60
            return self.calculate_suites_from_evg_stats(evg_stats, target_execution_time_secs)
        except requests.HTTPError as err:
            if err.response.status_code == requests.codes.SERVICE_UNAVAILABLE:
                # Evergreen may return a 503 when the service is degraded.
                # We fall back to splitting the tests into a fixed number of suites.
                LOGGER.warning("Received 503 from Evergreen, "
                               "dividing the tests evenly among suites")
                return self.calculate_fallback_suites()
            else:
                raise

    def get_evg_stats(self, project: str, start_date: datetime, end_date: datetime, task: str,
                      variant: str) -> List[TestStats]:
        """
        Collect test execution statistics data from Evergreen.

        :param project: Evergreen project to query.
        :param start_date: Time to start historical analysis.
        :param end_date: Time to end historical analysis.
        :param task: Task to query.
        :param variant: Build variant to query.
        :return: List of test stats for specified task.
        """
        # pylint: disable=too-many-arguments

        days = (end_date - start_date).days
        return self.evergreen_api.test_stats_by_project(
            project, after_date=start_date.strftime("%Y-%m-%d"),
            before_date=end_date.strftime("%Y-%m-%d"), tasks=[task], variants=[variant],
            group_by="test", group_num_days=days)

    def calculate_suites_from_evg_stats(self, data: List[TestStats],
                                        execution_time_secs: int) -> List[Suite]:
        """
        Divide tests into suites that can be run in less than the specified execution time.

        :param data: Historical test results for task being split.
        :param execution_time_secs: Target execution time of each suite (in seconds).
        :return: List of sub suites calculated.
        """
        test_stats = teststats.TestStats(data)
        tests_runtimes = self.filter_tests(test_stats.get_tests_runtimes())
        if not tests_runtimes:
            LOGGER.debug("No test runtimes after filter, using fallback")
            return self.calculate_fallback_suites()
        self.test_list = [info.test_name for info in tests_runtimes]
        return divide_tests_into_suites(self.config_options.suite, tests_runtimes,
                                        execution_time_secs, self.config_options.max_sub_suites,
                                        self.config_options.max_tests_per_suite)

    def filter_tests(self,
                     tests_runtimes: List[teststats.TestRuntime]) -> List[teststats.TestRuntime]:
        """
        Filter relevant tests.

        :param tests_runtimes: List of tuples containing test names and test runtimes.
        :return: Filtered TestRuntime objects indicating tests to be run.
        """
        tests_runtimes = self.filter_existing_tests(tests_runtimes)
        if self.config_options.selected_tests_to_run:
            tests_runtimes = filter_specified_tests(self.config_options.selected_tests_to_run,
                                                    tests_runtimes)
        return tests_runtimes

    def filter_existing_tests(self, tests_runtimes: List[teststats.TestRuntime]) \
            -> List[teststats.TestRuntime]:
        """Filter out tests that do not exist in the filesystem."""
        all_tests = [teststats.normalize_test_name(test) for test in self.list_tests()]
        return [
            info for info in tests_runtimes
            if os.path.exists(info.test_name) and info.test_name in all_tests
        ]

    def calculate_fallback_suites(self) -> List[Suite]:
        """Divide tests into a fixed number of suites."""
        LOGGER.debug("Splitting tasks based on fallback",
                     fallback=self.config_options.fallback_num_sub_suites)
        num_suites = self.config_options.fallback_num_sub_suites
        self.test_list = self.list_tests()
        suites = [Suite(self.config_options.suite) for _ in range(num_suites)]
        for idx, test_file in enumerate(self.test_list):
            suites[idx % num_suites].add_test(test_file, 0)
        return suites

    def list_tests(self) -> List[Dict]:
        """List the test files that are part of the suite being split."""
        return suitesconfig.get_suite(self.config_options.suite).tests

    def add_suites_to_build_variant(self, suites: List[Suite], build_variant: BuildVariant) -> None:
        """
        Add the given suites to the build variant specified.

        :param suites: Suites to add.
        :param build_variant: Build variant to add suite to.
        """
        EvergreenConfigGenerator(suites, self.config_options, self.evergreen_api) \
            .generate_config(build_variant)

    def generate_task_config(self, suites: List[Suite]) -> BuildVariant:
        """
        Generate the evergreen configuration for the new suite.

        :param suites: The suite the generated Evergreen config will be generated for.
        """
        build_variant = BuildVariant(self.config_options.variant)
        self.add_suites_to_build_variant(suites, build_variant)
        return build_variant

    def generate_suites_config(self, suites: List[Suite]) -> Dict:
        """
        Generate the suites files and evergreen configuration for the generated task.

        :return: The suites files and evergreen configuration for the generated task.
        """
        return render_suite_files(suites, self.config_options.suite, self.test_list,
                                  self.config_options.test_suites_dir,
                                  self.config_options.create_misc_suite)

    def get_suites(self) -> List[Suite]:
        """
        Generate the suites files and evergreen configuration for the generated task.

        :return: The suites files and evergreen configuration for the generated task.
        """
        end_date = datetime.datetime.utcnow().replace(microsecond=0)
        start_date = end_date - datetime.timedelta(days=LOOKBACK_DURATION_DAYS)
        return self.calculate_suites(start_date, end_date)

    def run(self):
        """Generate resmoke suites that run within a target execution time and write to disk."""
        LOGGER.debug("config options", config_options=self.config_options)
        if not should_tasks_be_generated(self.evergreen_api, self.config_options.task_id):
            LOGGER.info("Not generating configuration due to previous successful generation.")
            return

        suites = self.get_suites()
        LOGGER.debug("Creating suites", num_suites=len(suites), task=self.config_options.task,
                     dir=self.config_options.generated_config_dir)

        config_dict_of_suites = self.generate_suites_config(suites)

        shrub_config = ShrubProject.empty()
        shrub_config.add_build_variant(self.generate_task_config(suites))

        config_dict_of_suites[self.config_options.task + ".json"] = shrub_config.json()
        write_file_dict(self.config_options.generated_config_dir, config_dict_of_suites)


def filter_specified_tests(specified_tests: Set[str], tests_runtimes: List[teststats.TestRuntime]):
    """
    Filter out tests that have not been specified in the specified tests config option.

    :param specified_tests: List of test files that should be run.
    :param tests_runtimes: List of tuples containing test names and test runtimes.
    :return: List of TestRuntime tuples that match specified_tests.
    """
    return [info for info in tests_runtimes if info.test_name in specified_tests]


@click.command()
@click.option("--expansion-file", type=str, required=True,
              help="Location of expansions file generated by evergreen.")
@click.option("--evergreen-config", type=str, default=CONFIG_FILE,
              help="Location of evergreen configuration file.")
@click.option("--verbose", is_flag=True, default=False, help="Enable verbose logging.")
def main(expansion_file, evergreen_config, verbose):
    """
    Create a configuration for generate tasks to create sub suites for the specified resmoke suite.

    The `--expansion-file` should contain all the configuration needed to generate the tasks.
    \f
    :param expansion_file: Configuration file.
    :param evergreen_config: Evergreen configuration file.
    :param verbose: Use verbose logging.
    """
    enable_logging(verbose)
    evg_api = RetryingEvergreenApi.get_api(config_file=evergreen_config)
    config_options = ConfigOptions.from_file(expansion_file, REQUIRED_CONFIG_KEYS,
                                             DEFAULT_CONFIG_VALUES, CONFIG_FORMAT_FN)

    GenerateSubSuites(evg_api, config_options).run()


if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
