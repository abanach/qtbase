#!/usr/bin/env python3
#############################################################################
##
## Copyright (C) 2018 The Qt Company Ltd.
## Contact: https://www.qt.io/licensing/
##
## This file is part of the plugins of the Qt Toolkit.
##
## $QT_BEGIN_LICENSE:GPL-EXCEPT$
## Commercial License Usage
## Licensees holding valid commercial Qt licenses may use this file in
## accordance with the commercial license agreement provided with the
## Software or, alternatively, in accordance with the terms contained in
## a written agreement between you and The Qt Company. For licensing terms
## and conditions see https://www.qt.io/terms-conditions. For further
## information use the contact form at https://www.qt.io/contact-us.
##
## GNU General Public License Usage
## Alternatively, this file may be used under the terms of the GNU
## General Public License version 3 as published by the Free Software
## Foundation with exceptions as appearing in the file LICENSE.GPL3-EXCEPT
## included in the packaging of this file. Please review the following
## information to ensure the GNU General Public License requirements will
## be met: https://www.gnu.org/licenses/gpl-3.0.html.
##
## $QT_END_LICENSE$
##
#############################################################################


from __future__ import annotations

import copy
import os.path
import posixpath
import sys
import re
import io
import glob
import collections

try:
    collectionsAbc = collections.abc
except AttributeError:
    collectionsAbc = collections

import pyparsing as pp
import xml.etree.ElementTree as ET

from argparse import ArgumentParser
from textwrap import dedent
from itertools import chain
from shutil import copyfile
from sympy.logic import simplify_logic, And, Or, Not
from sympy.core.sympify import SympifyError
from typing import List, Optional, Dict, Set, IO, Union, Mapping, Any, Callable, FrozenSet, Tuple
from special_case_helper import SpecialCaseHandler
from helper import (
    map_qt_library,
    map_3rd_party_library,
    is_known_3rd_party_library,
    featureName,
    map_platform,
    find_library_info_for_target,
    generate_find_package_info,
    LibraryMapping,
)
from helper import _set_up_py_parsing_nicer_debug_output

_set_up_py_parsing_nicer_debug_output(pp)


cmake_version_string = "3.15.0"


def _parse_commandline():
    parser = ArgumentParser(
        description="Generate CMakeLists.txt files from ." "pro files.",
        epilog="Requirements: pip install sympy pyparsing",
    )
    parser.add_argument(
        "--debug", dest="debug", action="store_true", help="Turn on all debug output"
    )
    parser.add_argument(
        "--debug-parser",
        dest="debug_parser",
        action="store_true",
        help="Print debug output from qmake parser.",
    )
    parser.add_argument(
        "--debug-parse-result",
        dest="debug_parse_result",
        action="store_true",
        help="Dump the qmake parser result.",
    )
    parser.add_argument(
        "--debug-parse-dictionary",
        dest="debug_parse_dictionary",
        action="store_true",
        help="Dump the qmake parser result as dictionary.",
    )
    parser.add_argument(
        "--debug-pro-structure",
        dest="debug_pro_structure",
        action="store_true",
        help="Dump the structure of the qmake .pro-file.",
    )
    parser.add_argument(
        "--debug-full-pro-structure",
        dest="debug_full_pro_structure",
        action="store_true",
        help="Dump the full structure of the qmake .pro-file " "(with includes).",
    )
    parser.add_argument(
        "--debug-special-case-preservation",
        dest="debug_special_case_preservation",
        action="store_true",
        help="Show all git commands and file copies.",
    )

    parser.add_argument(
        "--is-example",
        action="store_true",
        dest="is_example",
        help="Treat the input .pro file as an example.",
    )
    parser.add_argument(
        "-s",
        "--skip-special-case-preservation",
        dest="skip_special_case_preservation",
        action="store_true",
        help="Skips behavior to reapply " "special case modifications (requires git in PATH)",
    )
    parser.add_argument(
        "-k",
        "--keep-temporary-files",
        dest="keep_temporary_files",
        action="store_true",
        help="Don't automatically remove CMakeLists.gen.txt and other " "intermediate files.",
    )

    parser.add_argument(
        "files",
        metavar="<.pro/.pri file>",
        type=str,
        nargs="+",
        help="The .pro/.pri file to process",
    )

    return parser.parse_args()


def is_top_level_repo_project(project_file_path: str = "") -> bool:
    qmake_conf_path = find_qmake_conf(project_file_path)
    qmake_conf_dir_path = os.path.dirname(qmake_conf_path)
    project_dir_path = os.path.dirname(project_file_path)
    return qmake_conf_dir_path == project_dir_path


def is_top_level_repo_tests_project(project_file_path: str = "") -> bool:
    qmake_conf_path = find_qmake_conf(project_file_path)
    qmake_conf_dir_path = os.path.dirname(qmake_conf_path)
    project_dir_path = os.path.dirname(project_file_path)
    project_dir_name = os.path.basename(project_dir_path)
    maybe_same_level_dir_path = os.path.join(project_dir_path, "..")
    normalized_maybe_same_level_dir_path = os.path.normpath(maybe_same_level_dir_path)
    return (
        qmake_conf_dir_path == normalized_maybe_same_level_dir_path and project_dir_name == "tests"
    )


def is_top_level_repo_examples_project(project_file_path: str = "") -> bool:
    qmake_conf_path = find_qmake_conf(project_file_path)
    qmake_conf_dir_path = os.path.dirname(qmake_conf_path)
    project_dir_path = os.path.dirname(project_file_path)
    project_dir_name = os.path.basename(project_dir_path)
    maybe_same_level_dir_path = os.path.join(project_dir_path, "..")
    normalized_maybe_same_level_dir_path = os.path.normpath(maybe_same_level_dir_path)
    return (
        qmake_conf_dir_path == normalized_maybe_same_level_dir_path
        and project_dir_name == "examples"
    )


def is_example_project(project_file_path: str = "") -> bool:
    qmake_conf_path = find_qmake_conf(project_file_path)
    qmake_conf_dir_path = os.path.dirname(qmake_conf_path)

    project_relative_path = os.path.relpath(project_file_path, qmake_conf_dir_path)
    # If the project file is found in a subdir called 'examples'
    # relative to the repo source dir, then it must be an example, but
    # some examples contain 3rdparty libraries that do not need to be
    # built as examples.
    return (project_relative_path.startswith("examples")
            and "3rdparty" not in project_relative_path)


def find_qmake_conf(project_file_path: str = "") -> Optional[str]:
    if not os.path.isabs(project_file_path):
        print(
            f"Warning: could not find .qmake.conf file, given path is not an "
            f"absolute path: {project_file_path}"
        )
        return None

    cwd = os.path.dirname(project_file_path)
    file_name = ".qmake.conf"

    while os.path.isdir(cwd):
        maybe_file = posixpath.join(cwd, file_name)
        if os.path.isfile(maybe_file):
            return maybe_file
        else:
            cwd = os.path.dirname(cwd)

    return None


def process_qrc_file(
    target: str,
    filepath: str,
    base_dir: str = "",
    project_file_path: str = "",
    skip_qtquick_compiler: bool = False,
    retain_qtquick_compiler: bool = False,
    is_example: bool = False,
) -> str:
    assert target

    # Hack to handle QT_SOURCE_TREE. Assume currently that it's the same
    # as the qtbase source path.
    qt_source_tree_literal = "${QT_SOURCE_TREE}"
    if qt_source_tree_literal in filepath:
        qmake_conf = find_qmake_conf(project_file_path)

        if qmake_conf:
            qt_source_tree = os.path.dirname(qmake_conf)
            filepath = filepath.replace(qt_source_tree_literal, qt_source_tree)
        else:
            print(
                f"Warning, could not determine QT_SOURCE_TREE location while trying "
                f"to find: {filepath}"
            )

    resource_name = os.path.splitext(os.path.basename(filepath))[0]
    dir_name = os.path.dirname(filepath)
    base_dir = posixpath.join("" if base_dir == "." else base_dir, dir_name)

    # Small not very thorough check to see if this a shared qrc resource
    # pattern is mostly used by the tests.
    is_parent_path = dir_name.startswith("..")
    if not os.path.isfile(filepath):
        raise RuntimeError(f"Invalid file path given to process_qrc_file: {filepath}")

    tree = ET.parse(filepath)
    root = tree.getroot()
    assert root.tag == "RCC"

    output = ""

    resource_count = 0
    for resource in root:
        assert resource.tag == "qresource"
        lang = resource.get("lang", "")
        prefix = resource.get("prefix", "/")
        if not prefix.startswith("/"):
            prefix = "/" + prefix

        full_resource_name = resource_name + (str(resource_count) if resource_count > 0 else "")

        files: Dict[str, str] = {}
        for file in resource:
            path = file.text
            assert path

            # Get alias:
            alias = file.get("alias", "")
            # In cases where examples use shared resources, we set the alias
            # too the same name of the file, or the applications won't be
            # be able to locate the resource
            if not alias and is_parent_path:
                alias = path
            files[path] = alias

        output += write_add_qt_resource_call(
            target,
            full_resource_name,
            prefix,
            base_dir,
            lang,
            files,
            skip_qtquick_compiler,
            retain_qtquick_compiler,
            is_example,
        )
        resource_count += 1

    return output


def write_add_qt_resource_call(
    target: str,
    resource_name: str,
    prefix: Optional[str],
    base_dir: Optional[str],
    lang: Optional[str],
    files: Dict[str, str],
    skip_qtquick_compiler: bool,
    retain_qtquick_compiler: bool,
    is_example: bool,
) -> str:
    output = ""

    sorted_files = sorted(files.keys())

    assert sorted_files

    for source in sorted_files:
        alias = files[source]
        if alias:
            full_source = posixpath.join(base_dir, source)
            output += (
                f'set_source_files_properties("{full_source}"\n'
                f'    PROPERTIES QT_RESOURCE_ALIAS "{alias}"\n)\n'
            )

    # Quote file paths in case there are spaces.
    sorted_files_backup = sorted_files
    sorted_files = []
    for source in sorted_files_backup:
        if source.startswith("${"):
            sorted_files.append(source)
        else:
            sorted_files.append(f'"{source}"')

    file_list = "\n    ".join(sorted_files)
    output += f"set({resource_name}_resource_files\n    {file_list}\n)\n\n"
    file_list = f"${{{resource_name}_resource_files}}"
    if skip_qtquick_compiler:
        output += (
            f"set_source_files_properties(${{{resource_name}_resource_files}}"
            " PROPERTIES QT_SKIP_QUICKCOMPILER 1)\n\n"
        )

    if retain_qtquick_compiler:
        output += (
            f"set_source_files_properties(${{{resource_name}_resource_files}}"
            "PROPERTIES QT_RETAIN_QUICKCOMPILER 1)\n\n"
        )

    params = ""
    if lang:
        params += f'{spaces(1)}LANG\n{spaces(2)}"{lang}"\n'
    params += f'{spaces(1)}PREFIX\n{spaces(2)}"{prefix}"\n'
    if base_dir:
        params += f'{spaces(1)}BASE\n{spaces(2)}"{base_dir}"\n'
    add_resource_command = ""
    if is_example:
        add_resource_command = "qt6_add_resources"
    else:
        add_resource_command = "add_qt_resource"
    output += (
        f'{add_resource_command}({target} "{resource_name}"\n{params}{spaces(1)}FILES\n'
        f"{spaces(2)}{file_list}\n)\n"
    )

    return output


class QmlDirFileInfo:
    def __init__(self, file_path: str, type_name: str):
        self.file_path = file_path
        self.version = ""
        self.type_name = type_name
        self.internal = False
        self.singleton = False


class QmlDir:
    def __init__(self):
        self.module = ""
        self.plugin_name = ""
        self.plugin_path = ""
        self.classname = ""
        self.imports = []  # typing.List[str]
        self.type_names = {}  # typing.Dict[str, QmlDirFileInfo]
        self.type_infos = []  # typing.List[str]
        self.depends = []  # typing.List[[str,str]]
        self.designer_supported = False

    def __str__(self):
        str = "module: {}\n".format(self.module)
        str += "plugin: {} {}\n".format(self.plugin_name, self.plugin_path)
        str += "classname: {}\n".format(self.classname)
        str += "type_infos:{}\n".format("    \n".join(self.type_infos))
        str += "imports:{}\n".format("    \n".join(self.imports))
        str += "dependends: \n"
        for dep in self.depends:
            str += "    {} {}\n".format(dep[0], dep[1])
        str += "designer supported: {}\n".format(self.designer_supported)
        str += "type_names:\n"
        for key in self.type_names:
            file_info = self.type_names[key]
            str += "    type:{} version:{} path:{} internal:{} singleton:{}\n".format(
                file_info.type_name,
                file_info.version,
                file_info.type_name,
                file_info.file_path,
                file_info.internal,
                file_info.singleton,
            )
        return str

    def get_or_create_file_info(self, path: str, type_name: str) -> QmlDirFileInfo:
        if not path in self.type_names:
            self.type_names[path] = QmlDirFileInfo(path, type_name)
        qmldir_file = self.type_names[path]
        if qmldir_file.type_name != type_name:
            raise RuntimeError("Registered qmldir file type_name does not match.")
        return qmldir_file

    def handle_file_internal(self, type_name: str, path: str):
        qmldir_file = self.get_or_create_file_info(path, type_name)
        qmldir_file.internal = True

    def handle_file_singleton(self, type_name: str, version: str, path: str):
        qmldir_file = self.handle_file(type_name, version, path)
        qmldir_file.singleton = True

    def handle_file(self, type_name: str, version: str, path: str) -> QmlDirFileInfo:
        qmldir_file = self.get_or_create_file_info(path, type_name)
        qmldir_file.version = version
        qmldir_file.type_name = type_name
        qmldir_file.path = path
        return qmldir_file

    def from_file(self, path: str):
        f = open(path, "r")
        if not f:
            raise RuntimeError("Failed to open qmldir file at: {}".format(str))
        for line in f:
            if line.startswith("#"):
                continue
            line = line.strip().replace("\n", "")
            if len(line) == 0:
                continue

            entries = line.split(" ")
            if len(entries) == 0:
                raise RuntimeError("Unexpected QmlDir file line entry")
            if entries[0] == "module":
                self.module = entries[1]
            elif entries[0] == "[singleton]":
                self.handle_file_singleton(entries[1], entries[2], entries[3])
            elif entries[0] == "internal":
                self.handle_file_internal(entries[1], entries[2])
            elif entries[0] == "plugin":
                self.plugin_name = entries[1]
                if len(entries) > 2:
                    self.plugin_path = entries[2]
            elif entries[0] == "classname":
                self.classname = entries[1]
            elif entries[0] == "typeinfo":
                self.type_infos.append(entries[1])
            elif entries[0] == "depends":
                self.depends.append((entries[1], entries[2]))
            elif entries[0] == "designersupported":
                self.designer_supported = True
            elif entries[0] == "import":
                self.imports.append(entries[1])
            elif len(entries) == 3:
                self.handle_file(entries[0], entries[1], entries[2])
            else:
                raise RuntimeError("Uhandled qmldir entry {}".format(line))


def fixup_linecontinuation(contents: str) -> str:
    # Remove all line continuations, aka a backslash followed by
    # a newline character with an arbitrary amount of whitespace
    # between the backslash and the newline.
    # This greatly simplifies the qmake parsing grammar.
    contents = re.sub(r"([^\t ])\\[ \t]*\n", "\\1 ", contents)
    contents = re.sub(r"\\[ \t]*\n", "", contents)
    return contents


def fixup_comments(contents: str) -> str:
    # Get rid of completely commented out lines.
    # So any line which starts with a '#' char and ends with a new line
    # will be replaced by a single new line.
    #
    # This is needed because qmake syntax is weird. In a multi line
    # assignment (separated by backslashes and newlines aka
    # # \\\n ), if any of the lines are completely commented out, in
    # principle the assignment should fail.
    #
    # It should fail because you would have a new line separating
    # the previous value from the next value, and the next value would
    # not be interpreted as a value, but as a new token / operation.
    # qmake is lenient though, and accepts that, so we need to take
    # care of it as well, as if the commented line didn't exist in the
    # first place.

    contents = re.sub(r"\n#[^\n]*?\n", "\n", contents, re.DOTALL)
    return contents


def spaces(indent: int) -> str:
    return "    " * indent


def trim_leading_dot(file: str) -> str:
    while file.startswith("./"):
        file = file[2:]
    return file


def map_to_file(f: str, scope: Scope, *, is_include: bool = False) -> str:
    assert "$$" not in f

    if f.startswith("${"):  # Some cmake variable is prepended
        return f

    base_dir = scope.currentdir if is_include else scope.basedir
    f = posixpath.join(base_dir, f)

    return trim_leading_dot(f)


def handle_vpath(source: str, base_dir: str, vpath: List[str]) -> str:
    assert "$$" not in source

    if not source:
        return ""

    if not vpath:
        return source

    if os.path.exists(os.path.join(base_dir, source)):
        return source

    variable_pattern = re.compile(r"\$\{[A-Za-z0-9_]+\}")
    match = re.match(variable_pattern, source)
    if match:
        # a complex, variable based path, skipping validation
        # or resolving
        return source

    for v in vpath:
        fullpath = posixpath.join(v, source)
        if os.path.exists(fullpath):
            return trim_leading_dot(posixpath.relpath(fullpath, base_dir))

    print(f"    XXXX: Source {source}: Not found.")
    return f"{source}-NOTFOUND"


def flatten_list(l):
    """ Flattens an irregular nested list into a simple list."""
    for el in l:
        if isinstance(el, collectionsAbc.Iterable) and not isinstance(el, (str, bytes)):
            yield from flatten_list(el)
        else:
            yield el


def handle_function_value(group: pp.ParseResults):
    function_name = group[0]
    function_args = group[1]
    if function_name == "qtLibraryTarget":
        if len(function_args) > 1:
            raise RuntimeError(
                "Don't know what to with more than one function argument "
                "for $$qtLibraryTarget()."
            )
        return str(function_args[0])

    if function_name == "quote":
        # Do nothing, just return a string result
        return str(group)

    if function_name == "files":
        if len(function_args) > 1:
            raise RuntimeError(
                "Don't know what to with more than one function argument for $$files()."
            )
        return str(function_args[0])

    if isinstance(function_args, pp.ParseResults):
        function_args = list(flatten_list(function_args.asList()))

    # Return the whole expression as a string.
    if function_name in [
        "join",
        "files",
        "cmakeRelativePath",
        "shell_quote",
        "shadowed",
        "cmakeTargetPath",
        "shell_path",
        "cmakeProcessLibs",
        "cmakeTargetPaths",
        "cmakePortablePaths",
        "escape_expand",
        "member",
    ]:
        return f"join({''.join(function_args)})"


class Operation:
    def __init__(self, value: Union[List[str], str]):
        if isinstance(value, list):
            self._value = value
        else:
            self._value = [str(value)]

    def process(
        self, key: str, input: List[str], transformer: Callable[[List[str]], List[str]]
    ) -> List[str]:
        assert False

    def __repr__(self):
        assert False

    def _dump(self):
        if not self._value:
            return "<NOTHING>"

        if not isinstance(self._value, list):
            return "<NOT A LIST>"

        result = []
        for i in self._value:
            if not i:
                result.append("<NONE>")
            else:
                result.append(str(i))
        return '"' + '", "'.join(result) + '"'


class AddOperation(Operation):
    def process(
        self, key: str, input: List[str], transformer: Callable[[List[str]], List[str]]
    ) -> List[str]:
        return input + transformer(self._value)

    def __repr__(self):
        return f"+({self._dump()})"


class UniqueAddOperation(Operation):
    def process(
        self, key: str, input: List[str], transformer: Callable[[List[str]], List[str]]
    ) -> List[str]:
        result = input
        for v in transformer(self._value):
            if v not in result:
                result.append(v)
        return result

    def __repr__(self):
        return f"*({self._dump()})"


class SetOperation(Operation):
    def process(
        self, key: str, input: List[str], transformer: Callable[[List[str]], List[str]]
    ) -> List[str]:
        values = []  # List[str]
        for v in self._value:
            if v != f"$${key}":
                values.append(v)
            else:
                values += input

        if transformer:
            return list(transformer(values))
        else:
            return values

    def __repr__(self):
        return f"=({self._dump()})"


class RemoveOperation(Operation):
    def __init__(self, value):
        super().__init__(value)

    def process(
        self, key: str, input: List[str], transformer: Callable[[List[str]], List[str]]
    ) -> List[str]:
        input_set = set(input)
        value_set = set(self._value)
        result: List[str] = []

        # Add everything that is not going to get removed:
        for v in input:
            if v not in value_set:
                result += [v]

        # Add everything else with removal marker:
        for v in transformer(self._value):
            if v not in input_set:
                result += [f"-{v}"]

        return result

    def __repr__(self):
        return f"-({self._dump()})"


class Scope(object):

    SCOPE_ID: int = 1

    def __init__(
        self,
        *,
        parent_scope: Optional[Scope],
        file: Optional[str] = None,
        condition: str = "",
        base_dir: str = "",
        operations: Union[Mapping[str, List[Operation]], None] = None,
    ) -> None:
        if operations is None:
            operations = {
                "QT_SOURCE_TREE": [SetOperation(["${QT_SOURCE_TREE}"])],
                "QT_BUILD_TREE": [SetOperation(["${PROJECT_BUILD_DIR}"])],
            }

        self._operations = copy.deepcopy(operations)
        if parent_scope:
            parent_scope._add_child(self)
        else:
            self._parent = None  # type: Optional[Scope]
            # Only add the  "QT = core gui" Set operation once, on the
            # very top-level .pro scope, aka it's basedir is empty.
            if not base_dir:
                self._operations["QT"] = [SetOperation(["core", "gui"])]

        self._basedir = base_dir
        if file:
            self._currentdir = os.path.dirname(file)
        if not self._currentdir:
            self._currentdir = "."
        if not self._basedir:
            self._basedir = self._currentdir

        self._scope_id = Scope.SCOPE_ID
        Scope.SCOPE_ID += 1
        self._file = file
        self._file_absolute_path = os.path.abspath(file)
        self._condition = map_condition(condition)
        self._children = []  # type: List[Scope]
        self._included_children = []  # type: List[Scope]
        self._visited_keys = set()  # type: Set[str]
        self._total_condition = None  # type: Optional[str]

    def __repr__(self):
        return (
            f"{self._scope_id}:{self._basedir}:{self._currentdir}:{self._file}:"
            f"{self._condition or '<TRUE>'}"
        )

    def reset_visited_keys(self):
        self._visited_keys = set()

    def merge(self, other: "Scope") -> None:
        assert self != other
        self._included_children.append(other)

    @property
    def scope_debug(self) -> bool:
        merge = self.get_string("PRO2CMAKE_SCOPE_DEBUG").lower()
        return merge == "1" or merge == "on" or merge == "yes" or merge == "true"

    @property
    def parent(self) -> Optional[Scope]:
        return self._parent

    @property
    def basedir(self) -> str:
        return self._basedir

    @property
    def currentdir(self) -> str:
        return self._currentdir

    def can_merge_condition(self):
        if self._condition == "else":
            return False
        if self._operations:
            return False

        child_count = len(self._children)
        if child_count == 0 or child_count > 2:
            return False
        assert child_count != 1 or self._children[0]._condition != "else"
        return child_count == 1 or self._children[1]._condition == "else"

    def settle_condition(self):
        new_children: List[Scope] = []
        for c in self._children:
            c.settle_condition()

            if c.can_merge_condition():
                child = c._children[0]
                child._condition = "({c._condition}) AND ({child._condition})"
                new_children += c._children
            else:
                new_children.append(c)
        self._children = new_children

    @staticmethod
    def FromDict(
        parent_scope: Optional["Scope"], file: str, statements, cond: str = "", base_dir: str = ""
    ) -> Scope:
        scope = Scope(parent_scope=parent_scope, file=file, condition=cond, base_dir=base_dir)
        for statement in statements:
            if isinstance(statement, list):  # Handle skipped parts...
                assert not statement
                continue

            operation = statement.get("operation", None)
            if operation:
                key = statement.get("key", "")
                value = statement.get("value", [])
                assert key != ""

                if operation == "=":
                    scope._append_operation(key, SetOperation(value))
                elif operation == "-=":
                    scope._append_operation(key, RemoveOperation(value))
                elif operation == "+=":
                    scope._append_operation(key, AddOperation(value))
                elif operation == "*=":
                    scope._append_operation(key, UniqueAddOperation(value))
                else:
                    print(f'Unexpected operation "{operation}" in scope "{scope}".')
                    assert False

                continue

            condition = statement.get("condition", None)
            if condition:
                Scope.FromDict(scope, file, statement.get("statements"), condition, scope.basedir)

                else_statements = statement.get("else_statements")
                if else_statements:
                    Scope.FromDict(scope, file, else_statements, "else", scope.basedir)
                continue

            loaded = statement.get("loaded")
            if loaded:
                scope._append_operation("_LOADED", UniqueAddOperation(loaded))
                continue

            option = statement.get("option", None)
            if option:
                scope._append_operation("_OPTION", UniqueAddOperation(option))
                continue

            included = statement.get("included", None)
            if included:
                scope._append_operation("_INCLUDED", UniqueAddOperation(included))
                continue

        scope.settle_condition()

        if scope.scope_debug:
            print(f"..... [SCOPE_DEBUG]: Created scope {scope}:")
            scope.dump(indent=1)
            print("..... [SCOPE_DEBUG]: <<END OF SCOPE>>")
        return scope

    def _append_operation(self, key: str, op: Operation) -> None:
        if key in self._operations:
            self._operations[key].append(op)
        else:
            self._operations[key] = [op]

    @property
    def file(self) -> str:
        return self._file or ""

    @property
    def file_absolute_path(self) -> str:
        return self._file_absolute_path or ""

    @property
    def generated_cmake_lists_path(self) -> str:
        assert self.basedir
        return os.path.join(self.basedir, "CMakeLists.gen.txt")

    @property
    def original_cmake_lists_path(self) -> str:
        assert self.basedir
        return os.path.join(self.basedir, "CMakeLists.txt")

    @property
    def condition(self) -> str:
        return self._condition

    @property
    def total_condition(self) -> Optional[str]:
        return self._total_condition

    @total_condition.setter
    def total_condition(self, condition: str) -> None:
        self._total_condition = condition

    def _add_child(self, scope: "Scope") -> None:
        scope._parent = self
        self._children.append(scope)

    @property
    def children(self) -> List["Scope"]:
        result = list(self._children)
        for include_scope in self._included_children:
            result += include_scope.children
        return result

    def dump(self, *, indent: int = 0) -> None:
        ind = spaces(indent)
        print(f'{ind}Scope "{self}":')
        if self.total_condition:
            print(f"{ind}  Total condition = {self.total_condition}")
        print(f"{ind}  Keys:")
        keys = self._operations.keys()
        if not keys:
            print(f"{ind}    -- NONE --")
        else:
            for k in sorted(keys):
                print(f'{ind}    {k} = "{self._operations.get(k, [])}"')
        print(f"{ind}  Children:")
        if not self._children:
            print(f"{ind}    -- NONE --")
        else:
            for c in self._children:
                c.dump(indent=indent + 1)
        print(f"{ind}  Includes:")
        if not self._included_children:
            print(f"{ind}    -- NONE --")
        else:
            for c in self._included_children:
                c.dump(indent=indent + 1)

    def dump_structure(self, *, type: str = "ROOT", indent: int = 0) -> None:
        print(f"{spaces(indent)}{type}: {self}")
        for i in self._included_children:
            i.dump_structure(type="INCL", indent=indent + 1)
        for i in self._children:
            i.dump_structure(type="CHLD", indent=indent + 1)

    @property
    def keys(self):
        return self._operations.keys()

    @property
    def visited_keys(self):
        return self._visited_keys

    def _evalOps(
        self,
        key: str,
        transformer: Optional[Callable[[Scope, List[str]], List[str]]],
        result: List[str],
        *,
        inherit: bool = False,
    ) -> List[str]:
        self._visited_keys.add(key)

        # Inherrit values from above:
        if self._parent and inherit:
            result = self._parent._evalOps(key, transformer, result)

        if transformer:

            def op_transformer(files):
                return transformer(self, files)

        else:

            def op_transformer(files):
                return files

        for op in self._operations.get(key, []):
            result = op.process(key, result, op_transformer)

        for ic in self._included_children:
            result = list(ic._evalOps(key, transformer, result))

        return result

    def get(self, key: str, *, ignore_includes: bool = False, inherit: bool = False) -> List[str]:

        is_same_path = self.currentdir == self.basedir
        if not is_same_path:
            relative_path = os.path.relpath(self.currentdir, self.basedir)

        if key == "_PRO_FILE_PWD_":
            return ["${CMAKE_CURRENT_SOURCE_DIR}"]
        if key == "PWD":
            if is_same_path:
                return ["${CMAKE_CURRENT_SOURCE_DIR}"]
            else:
                return [f"${{CMAKE_CURRENT_SOURCE_DIR}}/{relative_path}"]
        if key == "OUT_PWD":
            if is_same_path:
                return ["${CMAKE_CURRENT_BINARY_DIR}"]
            else:
                return [f"${{CMAKE_CURRENT_BINARY_DIR}}/{relative_path}"]

        return self._evalOps(key, None, [], inherit=inherit)

    def get_string(self, key: str, default: str = "", inherit: bool = False) -> str:
        v = self.get(key, inherit=inherit)
        if len(v) == 0:
            return default
        assert len(v) == 1
        return v[0]

    def _map_files(
        self, files: List[str], *, use_vpath: bool = True, is_include: bool = False
    ) -> List[str]:

        expanded_files = []  # type: List[str]
        for f in files:
            r = self._expand_value(f)
            expanded_files += r

        mapped_files = list(
            map(lambda f: map_to_file(f, self, is_include=is_include), expanded_files)
        )

        if use_vpath:
            result = list(
                map(
                    lambda f: handle_vpath(f, self.basedir, self.get("VPATH", inherit=True)),
                    mapped_files,
                )
            )
        else:
            result = mapped_files

        # strip ${CMAKE_CURRENT_SOURCE_DIR}:
        result = list(
            map(lambda f: f[28:] if f.startswith("${CMAKE_CURRENT_SOURCE_DIR}/") else f, result)
        )

        # strip leading ./:
        result = list(map(lambda f: trim_leading_dot(f), result))

        return result

    def get_files(
        self, key: str, *, use_vpath: bool = False, is_include: bool = False
    ) -> List[str]:
        def transformer(scope, files):
            return scope._map_files(files, use_vpath=use_vpath, is_include=is_include)

        return list(self._evalOps(key, transformer, []))

    @staticmethod
    def _replace_env_var_value(value: Any) -> str:
        if not isinstance(value, str):
            return value

        pattern = re.compile(r"\$\$\(?([A-Za-z_][A-Za-z0-9_]*)\)?")
        match = re.search(pattern, value)
        if match:
            value = re.sub(pattern, r"$ENV{\1}", value)

        return value

    def _expand_value(self, value: str) -> List[str]:
        result = value
        pattern = re.compile(r"\$\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
        match = re.search(pattern, result)
        while match:
            old_result = result
            match_group_0 = match.group(0)
            if match_group_0 == value:
                get_result = self.get(match.group(1), inherit=True)
                if len(get_result) == 1:
                    result = get_result[0]
                    result = self._replace_env_var_value(result)
                else:
                    # Recursively expand each value from the result list
                    # returned from self.get().
                    result_list = []
                    for entry_value in get_result:
                        result_list += self._expand_value(self._replace_env_var_value(entry_value))
                    return result_list
            else:
                replacement = self.get(match.group(1), inherit=True)
                replacement_str = replacement[0] if replacement else ""
                result = result[: match.start()] + replacement_str + result[match.end() :]
                result = self._replace_env_var_value(result)

            if result == old_result:
                return [result]  # Do not go into infinite loop

            match = re.search(pattern, result)

        result = self._replace_env_var_value(result)
        return [result]

    def expand(self, key: str) -> List[str]:
        value = self.get(key)
        result: List[str] = []
        assert isinstance(value, list)
        for v in value:
            result += self._expand_value(v)
        return result

    def expandString(self, key: str) -> str:
        result = self._expand_value(self.get_string(key))
        assert len(result) == 1
        return result[0]

    @property
    def TEMPLATE(self) -> str:
        return self.get_string("TEMPLATE", "app")

    def _rawTemplate(self) -> str:
        return self.get_string("TEMPLATE")

    @property
    def TARGET(self) -> str:
        target = self.expandString("TARGET") or os.path.splitext(os.path.basename(self.file))[0]
        return re.sub(r"\.\./", "", target)

    @property
    def _INCLUDED(self) -> List[str]:
        return self.get("_INCLUDED")


class QmakeParser:
    def __init__(self, *, debug: bool = False) -> None:
        self.debug = debug
        self._Grammar = self._generate_grammar()

    def _generate_grammar(self):
        # Define grammar:
        pp.ParserElement.setDefaultWhitespaceChars(" \t")

        def add_element(name: str, value: pp.ParserElement):
            nonlocal self
            if self.debug:
                value.setName(name)
                value.setDebug()
            return value

        EOL = add_element("EOL", pp.Suppress(pp.LineEnd()))
        Else = add_element("Else", pp.Keyword("else"))
        Identifier = add_element(
            "Identifier", pp.Word(f"{pp.alphas}_", bodyChars=pp.alphanums + "_-./")
        )
        BracedValue = add_element(
            "BracedValue",
            pp.nestedExpr(
                ignoreExpr=pp.quotedString
                | pp.QuotedString(
                    quoteChar="$(", endQuoteChar=")", escQuote="\\", unquoteResults=False
                )
            ).setParseAction(lambda s, l, t: ["(", *t[0], ")"]),
        )

        Substitution = add_element(
            "Substitution",
            pp.Combine(
                pp.Literal("$")
                + (
                    (
                        (pp.Literal("$") + Identifier + pp.Optional(pp.nestedExpr()))
                        | (pp.Literal("(") + Identifier + pp.Literal(")"))
                        | (pp.Literal("{") + Identifier + pp.Literal("}"))
                        | (
                            pp.Literal("$")
                            + pp.Literal("{")
                            + Identifier
                            + pp.Optional(pp.nestedExpr())
                            + pp.Literal("}")
                        )
                        | (pp.Literal("$") + pp.Literal("[") + Identifier + pp.Literal("]"))
                    )
                )
            ),
        )
        LiteralValuePart = add_element(
            "LiteralValuePart", pp.Word(pp.printables, excludeChars="$#{}()")
        )
        SubstitutionValue = add_element(
            "SubstitutionValue",
            pp.Combine(pp.OneOrMore(Substitution | LiteralValuePart | pp.Literal("$"))),
        )
        FunctionValue = add_element(
            "FunctionValue",
            pp.Group(
                pp.Suppress(pp.Literal("$") + pp.Literal("$"))
                + Identifier
                + pp.nestedExpr()  # .setParseAction(lambda s, l, t: ['(', *t[0], ')'])
            ).setParseAction(lambda s, l, t: handle_function_value(*t)),
        )
        Value = add_element(
            "Value",
            pp.NotAny(Else | pp.Literal("}") | EOL)
            + (
                pp.QuotedString(quoteChar='"', escChar="\\")
                | FunctionValue
                | SubstitutionValue
                | BracedValue
            ),
        )

        Values = add_element("Values", pp.ZeroOrMore(Value)("value"))

        Op = add_element(
            "OP", pp.Literal("=") | pp.Literal("-=") | pp.Literal("+=") | pp.Literal("*=")
        )

        Key = add_element("Key", Identifier)

        Operation = add_element("Operation", Key("key") + Op("operation") + Values("value"))
        CallArgs = add_element("CallArgs", pp.nestedExpr())

        def parse_call_args(results):
            out = ""
            for item in chain(*results):
                if isinstance(item, str):
                    out += item
                else:
                    out += "(" + parse_call_args(item) + ")"
            return out

        CallArgs.setParseAction(parse_call_args)

        Load = add_element("Load", pp.Keyword("load") + CallArgs("loaded"))
        Include = add_element("Include", pp.Keyword("include") + CallArgs("included"))
        Option = add_element("Option", pp.Keyword("option") + CallArgs("option"))

        # ignore the whole thing...
        DefineTestDefinition = add_element(
            "DefineTestDefinition",
            pp.Suppress(
                pp.Keyword("defineTest")
                + CallArgs
                + pp.nestedExpr(opener="{", closer="}", ignoreExpr=pp.LineEnd())
            ),
        )

        # ignore the whole thing...
        ForLoop = add_element(
            "ForLoop",
            pp.Suppress(
                pp.Keyword("for")
                + CallArgs
                + pp.nestedExpr(opener="{", closer="}", ignoreExpr=pp.LineEnd())
            ),
        )

        # ignore the whole thing...
        ForLoopSingleLine = add_element(
            "ForLoopSingleLine",
            pp.Suppress(pp.Keyword("for") + CallArgs + pp.Literal(":") + pp.SkipTo(EOL)),
        )

        # ignore the whole thing...
        FunctionCall = add_element("FunctionCall", pp.Suppress(Identifier + pp.nestedExpr()))

        Scope = add_element("Scope", pp.Forward())

        Statement = add_element(
            "Statement",
            pp.Group(
                Load
                | Include
                | Option
                | ForLoop
                | ForLoopSingleLine
                | DefineTestDefinition
                | FunctionCall
                | Operation
            ),
        )
        StatementLine = add_element("StatementLine", Statement + (EOL | pp.FollowedBy("}")))
        StatementGroup = add_element(
            "StatementGroup", pp.ZeroOrMore(StatementLine | Scope | pp.Suppress(EOL))
        )

        Block = add_element(
            "Block",
            pp.Suppress("{")
            + pp.Optional(EOL)
            + StatementGroup
            + pp.Optional(EOL)
            + pp.Suppress("}")
            + pp.Optional(EOL),
        )

        ConditionEnd = add_element(
            "ConditionEnd",
            pp.FollowedBy(
                (pp.Optional(pp.White()) + (pp.Literal(":") | pp.Literal("{") | pp.Literal("|")))
            ),
        )

        ConditionPart1 = add_element(
            "ConditionPart1", (pp.Optional("!") + Identifier + pp.Optional(BracedValue))
        )
        ConditionPart2 = add_element("ConditionPart2", pp.CharsNotIn("#{}|:=\\\n"))
        ConditionPart = add_element(
            "ConditionPart", (ConditionPart1 ^ ConditionPart2) + ConditionEnd
        )

        ConditionOp = add_element("ConditionOp", pp.Literal("|") ^ pp.Literal(":"))
        ConditionWhiteSpace = add_element(
            "ConditionWhiteSpace", pp.Suppress(pp.Optional(pp.White(" ")))
        )

        ConditionRepeated = add_element(
            "ConditionRepeated", pp.ZeroOrMore(ConditionOp + ConditionWhiteSpace + ConditionPart)
        )

        Condition = add_element("Condition", pp.Combine(ConditionPart + ConditionRepeated))
        Condition.setParseAction(lambda x: " ".join(x).strip().replace(":", " && ").strip(" && "))

        # Weird thing like write_file(a)|error() where error() is the alternative condition
        # which happens to be a function call. In this case there is no scope, but our code expects
        # a scope with a list of statements, so create a fake empty statement.
        ConditionEndingInFunctionCall = add_element(
            "ConditionEndingInFunctionCall",
            pp.Suppress(ConditionOp)
            + FunctionCall
            + pp.Empty().setParseAction(lambda x: [[]]).setResultsName("statements"),
        )

        SingleLineScope = add_element(
            "SingleLineScope",
            pp.Suppress(pp.Literal(":")) + pp.Group(Block | (Statement + EOL))("statements"),
        )
        MultiLineScope = add_element("MultiLineScope", Block("statements"))

        SingleLineElse = add_element(
            "SingleLineElse",
            pp.Suppress(pp.Literal(":")) + (Scope | Block | (Statement + pp.Optional(EOL))),
        )
        MultiLineElse = add_element("MultiLineElse", Block)
        ElseBranch = add_element("ElseBranch", pp.Suppress(Else) + (SingleLineElse | MultiLineElse))

        # Scope is already add_element'ed in the forward declaration above.
        Scope <<= pp.Group(
            Condition("condition")
            + (SingleLineScope | MultiLineScope | ConditionEndingInFunctionCall)
            + pp.Optional(ElseBranch)("else_statements")
        )

        Grammar = StatementGroup("statements")
        Grammar.ignore(pp.pythonStyleComment())

        return Grammar

    def parseFile(self, file: str):
        print(f'Parsing "{file}"...')
        try:
            with open(file, "r") as file_fd:
                contents = file_fd.read()

            # old_contents = contents
            contents = fixup_comments(contents)
            contents = fixup_linecontinuation(contents)
            result = self._Grammar.parseString(contents, parseAll=True)
        except pp.ParseException as pe:
            print(pe.line)
            print(f"{' ' * (pe.col-1)}^")
            print(pe)
            raise pe
        return result


def parseProFile(file: str, *, debug=False):
    parser = QmakeParser(debug=debug)
    return parser.parseFile(file)


def map_condition(condition: str) -> str:
    # Some hardcoded cases that are too bothersome to generalize.
    condition = re.sub(
        r"qtConfig\(opengl\(es1\|es2\)\?\)",
        r"QT_FEATURE_opengl OR QT_FEATURE_opengles2 OR QT_FEATURE_opengles3",
        condition,
    )
    condition = re.sub(r"qtConfig\(opengl\.\*\)", r"QT_FEATURE_opengl", condition)
    condition = re.sub(r"^win\*$", r"win", condition)
    condition = re.sub(r"^no-png$", r"NOT QT_FEATURE_png", condition)
    condition = re.sub(r"contains\(CONFIG, static\)", r"NOT QT_BUILD_SHARED_LIBS", condition)
    condition = re.sub(r"contains\(QT_CONFIG,\w*shared\)", r"QT_BUILD_SHARED_LIBS", condition)

    def gcc_version_handler(match_obj: re.Match):
        operator = match_obj.group(1)
        version_type = match_obj.group(2)
        if operator == "equals":
            operator = "STREQUAL"
        elif operator == "greaterThan":
            operator = "STRGREATER"
        elif operator == "lessThan":
            operator = "STRLESS"

        version = match_obj.group(3)
        return f"(QT_COMPILER_VERSION_{version_type} {operator} {version})"

    # TODO: Possibly fix for other compilers.
    pattern = r"(equals|greaterThan|lessThan)\(QT_GCC_([A-Z]+)_VERSION,[ ]*([0-9]+)\)"
    condition = re.sub(pattern, gcc_version_handler, condition)

    # TODO: the current if(...) replacement makes the parentheses
    # unbalanced when there are nested expressions.
    # Need to fix this either with pypi regex recursive regexps,
    # using pyparsing, or some other proper means of handling
    # balanced parentheses.
    condition = re.sub(r"\bif\s*\((.*?)\)", r"\1", condition)

    condition = re.sub(r"\bisEmpty\s*\((.*?)\)", r"\1_ISEMPTY", condition)
    condition = re.sub(r'\bcontains\s*\((.*?),\s*"?(.*?)"?\)', r"\1___contains___\2", condition)
    condition = re.sub(r'\bequals\s*\((.*?),\s*"?(.*?)"?\)', r"\1___equals___\2", condition)
    condition = re.sub(r'\bisEqual\s*\((.*?),\s*"?(.*?)"?\)', r"\1___equals___\2", condition)
    condition = re.sub(r"\s*==\s*", "___STREQUAL___", condition)
    condition = re.sub(r"\bexists\s*\((.*?)\)", r"EXISTS \1", condition)

    pattern = r"CONFIG\((debug|release),debug\|release\)"
    match_result = re.match(pattern, condition)
    if match_result:
        build_type = match_result.group(1)
        if build_type == "debug":
            build_type = "Debug"
        elif build_type == "release":
            build_type = "Release"
        condition = re.sub(pattern, f"(CMAKE_BUILD_TYPE STREQUAL {build_type})", condition)

    condition = condition.replace("*", "_x_")
    condition = condition.replace(".$$", "__ss_")
    condition = condition.replace("$$", "_ss_")

    condition = condition.replace("!", "NOT ")
    condition = condition.replace("&&", " AND ")
    condition = condition.replace("|", " OR ")

    cmake_condition = ""
    for part in condition.split():
        # some features contain e.g. linux, that should not be
        # turned upper case
        feature = re.match(r"(qtConfig|qtHaveModule)\(([a-zA-Z0-9_-]+)\)", part)
        if feature:
            if feature.group(1) == "qtHaveModule":
                part = f"TARGET {map_qt_library(feature.group(2))}"
            else:
                feature_name = featureName(feature.group(2))
                if feature_name.startswith("system_") and is_known_3rd_party_library(
                    feature_name[7:]
                ):
                    part = "ON"
                elif feature == "dlopen":
                    part = "ON"
                else:
                    part = "QT_FEATURE_" + feature_name
        else:
            part = map_platform(part)

        part = part.replace("true", "ON")
        part = part.replace("false", "OFF")
        cmake_condition += " " + part
    return cmake_condition.strip()


def handle_subdir(
    scope: Scope, cm_fh: IO[str], *, indent: int = 0, is_example: bool = False
) -> None:

    # Global nested dictionary that will contain sub_dir assignments and their conditions.
    # Declared as a global in order not to pollute the nested function signatures with giant
    # type hints.
    sub_dirs: Dict[str, Dict[str, Set[FrozenSet[str]]]] = {}

    # Collects assignment conditions into global sub_dirs dict.
    def collect_subdir_info(sub_dir_assignment: str, *, current_conditions: FrozenSet[str] = None):
        subtraction = sub_dir_assignment.startswith("-")
        if subtraction:
            subdir_name = sub_dir_assignment[1:]
        else:
            subdir_name = sub_dir_assignment
        if subdir_name not in sub_dirs:
            sub_dirs[subdir_name] = {}
        additions = sub_dirs[subdir_name].get("additions", set())
        subtractions = sub_dirs[subdir_name].get("subtractions", set())
        if current_conditions:
            if subtraction:
                subtractions.add(current_conditions)
            else:
                additions.add(current_conditions)
        if additions:
            sub_dirs[subdir_name]["additions"] = additions
        if subtractions:
            sub_dirs[subdir_name]["subtractions"] = subtractions

    # Recursive helper that collects subdir info for given scope,
    # and the children of the given scope.
    def handle_subdir_helper(
        scope: Scope,
        cm_fh: IO[str],
        *,
        indent: int = 0,
        current_conditions: FrozenSet[str] = None,
        is_example: bool = False,
    ):
        for sd in scope.get_files("SUBDIRS"):
            # Collect info about conditions and SUBDIR assignments in the
            # current scope.
            if os.path.isdir(sd) or sd.startswith("-"):
                collect_subdir_info(sd, current_conditions=current_conditions)
            # For the file case, directly write into the file handle.
            elif os.path.isfile(sd):
                # Handle cases with SUBDIRS += Foo/bar/z.pro. We want to be able
                # to generate add_subdirectory(Foo/bar) instead of parsing the full
                # .pro file in the current CMakeLists.txt. This causes issues
                # with relative paths in certain projects otherwise.
                dirname = os.path.dirname(sd)
                if dirname:
                    collect_subdir_info(dirname, current_conditions=current_conditions)
                else:
                    subdir_result = parseProFile(sd, debug=False)
                    subdir_scope = Scope.FromDict(
                        scope, sd, subdir_result.asDict().get("statements"), "", scope.basedir
                    )

                    do_include(subdir_scope)
                    cmakeify_scope(subdir_scope, cm_fh, indent=indent, is_example=is_example)
            else:
                print(f"    XXXX: SUBDIR {sd} in {scope}: Not found.")

        # Collect info about conditions and SUBDIR assignments in child
        # scopes, aka recursively call the same function, but with an
        # updated current_conditions frozen set.
        for c in scope.children:
            # Use total_condition for 'else' conditions, otherwise just use the regular value to
            # simplify the logic.
            child_condition = c.total_condition if c.condition == "else" else c.condition
            handle_subdir_helper(
                c,
                cm_fh,
                indent=indent + 1,
                is_example=is_example,
                current_conditions=frozenset((*current_conditions, child_condition)),
            )

    def group_and_print_sub_dirs(indent: int = 0):
        # Simplify conditions, and group
        # subdirectories with the same conditions.
        grouped_sub_dirs = {}

        # Wraps each element in the given interable with parentheses,
        # to make sure boolean simplification happens correctly.
        def wrap_in_parenthesis(iterable):
            return [f"({c})" for c in iterable]

        def join_all_conditions(set_of_alternatives):
            # Elements within one frozen set represent one single
            # alternative whose pieces are ANDed together.
            # This is repeated for each alternative that would
            # enable a subdir, and are thus ORed together.
            final_str = ""
            if set_of_alternatives:
                wrapped_set_of_alternatives = [
                    wrap_in_parenthesis(alternative) for alternative in set_of_alternatives
                ]
                alternatives = [
                    f'({" AND ".join(alternative)})' for alternative in wrapped_set_of_alternatives
                ]
                final_str = " OR ".join(sorted(alternatives))
            return final_str

        for subdir_name in sub_dirs:
            additions = sub_dirs[subdir_name].get("additions", set())
            subtractions = sub_dirs[subdir_name].get("subtractions", set())

            # An empty condition key represents the group of sub dirs
            # that should be added unconditionally.
            condition_key = ""
            if additions or subtractions:
                addition_str = join_all_conditions(additions)
                if addition_str:
                    addition_str = f"({addition_str})"
                subtraction_str = join_all_conditions(subtractions)
                if subtraction_str:
                    subtraction_str = f"NOT ({subtraction_str})"

                condition_str = addition_str
                if condition_str and subtraction_str:
                    condition_str += " AND "
                condition_str += subtraction_str
                if not condition_str.rstrip("()").strip():
                    continue
                condition_simplified = simplify_condition(condition_str)
                condition_key = condition_simplified

            sub_dir_list_by_key = grouped_sub_dirs.get(condition_key, [])
            sub_dir_list_by_key.append(subdir_name)
            grouped_sub_dirs[condition_key] = sub_dir_list_by_key

        # Print the groups.
        ind = spaces(indent)
        for condition_key in grouped_sub_dirs:
            cond_ind = ind
            if condition_key:
                cm_fh.write(f"{ind}if({condition_key})\n")
                cond_ind += "    "

            sub_dir_list_by_key = grouped_sub_dirs.get(condition_key, [])
            for subdir_name in sub_dir_list_by_key:
                cm_fh.write(f"{cond_ind}add_subdirectory({subdir_name})\n")
            if condition_key:
                cm_fh.write(f"{ind}endif()\n")

    # A set of conditions which will be ANDed together. The set is recreated with more conditions
    # as the scope deepens.
    current_conditions = frozenset()

    # Compute the total condition for scopes. Needed for scopes that
    # have 'else' as a condition.
    recursive_evaluate_scope(scope)

    # Do the work.
    handle_subdir_helper(
        scope, cm_fh, indent=indent, current_conditions=current_conditions, is_example=is_example
    )
    group_and_print_sub_dirs(indent=indent)


def sort_sources(sources: List[str]) -> List[str]:
    to_sort = {}  # type: Dict[str, List[str]]
    for s in sources:
        if s is None:
            continue

        dir = os.path.dirname(s)
        base = os.path.splitext(os.path.basename(s))[0]
        if base.endswith("_p"):
            base = base[:-2]
        sort_name = posixpath.join(dir, base)

        array = to_sort.get(sort_name, [])
        array.append(s)

        to_sort[sort_name] = array

    lines = []
    for k in sorted(to_sort.keys()):
        lines.append(" ".join(sorted(to_sort[k])))

    return lines


def _map_libraries_to_cmake(libraries: List[str], known_libraries: Set[str]) -> List[str]:
    result = []  # type: List[str]
    is_framework = False

    for lib in libraries:
        if lib == "-framework":
            is_framework = True
            continue
        if is_framework:
            lib = f"${{FW{lib}}}"
        if lib.startswith("-l"):
            lib = lib[2:]

        if lib.startswith("-"):
            lib = f"# Remove: {lib[1:]}"
        else:
            lib = map_3rd_party_library(lib)

        if not lib or lib in result or lib in known_libraries:
            continue

        result.append(lib)
        is_framework = False

    return result


def extract_cmake_libraries(
    scope: Scope, *, known_libraries: Set[str] = set()
) -> Tuple[List[str], List[str]]:
    public_dependencies = []  # type: List[str]
    private_dependencies = []  # type: List[str]

    for key in ["QMAKE_USE", "LIBS"]:
        public_dependencies += scope.expand(key)
    for key in ["QMAKE_USE_PRIVATE", "QMAKE_USE_FOR_PRIVATE", "LIBS_PRIVATE"]:
        private_dependencies += scope.expand(key)

    for key in ["QT_FOR_PRIVATE", "QT_PRIVATE"]:
        private_dependencies += [map_qt_library(q) for q in scope.expand(key)]

    for key in ["QT"]:
        # Qt public libs: These may include FooPrivate in which case we get
        # a private dependency on FooPrivate as well as a public dependency on Foo
        for lib in scope.expand(key):
            mapped_lib = map_qt_library(lib)

            if mapped_lib.endswith("Private"):
                private_dependencies.append(mapped_lib)
                public_dependencies.append(mapped_lib[:-7])
            else:
                public_dependencies.append(mapped_lib)

    return (
        _map_libraries_to_cmake(public_dependencies, known_libraries),
        _map_libraries_to_cmake(private_dependencies, known_libraries),
    )


def write_header(cm_fh: IO[str], name: str, typename: str, *, indent: int = 0):
    ind = spaces(indent)
    comment_line = "#" * 69
    cm_fh.write(f"{ind}{comment_line}\n")
    cm_fh.write(f"{ind}## {name} {typename}:\n")
    cm_fh.write(f"{ind}{comment_line}\n\n")


def write_scope_header(cm_fh: IO[str], *, indent: int = 0):
    ind = spaces(indent)
    comment_line = "#" * 69
    cm_fh.write(f"\n{ind}## Scopes:\n")
    cm_fh.write(f"{ind}{comment_line}\n")


def write_list(
    cm_fh: IO[str],
    entries: List[str],
    cmake_parameter: str,
    indent: int = 0,
    *,
    header: str = "",
    footer: str = "",
):
    if not entries:
        return

    ind = spaces(indent)
    extra_indent = ""

    if header:
        cm_fh.write(f"{ind}{header}")
        extra_indent += "    "
    if cmake_parameter:
        cm_fh.write(f"{ind}{extra_indent}{cmake_parameter}\n")
        extra_indent += "    "
    for s in sort_sources(entries):
        cm_fh.write(f"{ind}{extra_indent}{s}\n")
    if footer:
        cm_fh.write(f"{ind}{footer}\n")


def write_source_file_list(
    cm_fh: IO[str],
    scope,
    cmake_parameter: str,
    keys: List[str],
    indent: int = 0,
    *,
    header: str = "",
    footer: str = "",
):
    # collect sources
    sources: List[str] = []
    for key in keys:
        sources += scope.get_files(key, use_vpath=True)

    write_list(cm_fh, sources, cmake_parameter, indent, header=header, footer=footer)


def write_all_source_file_lists(
    cm_fh: IO[str],
    scope: Scope,
    header: str,
    *,
    indent: int = 0,
    footer: str = "",
    extra_keys: Optional[List[str]] = None,
):
    if extra_keys is None:
        extra_keys = []
    write_source_file_list(
        cm_fh,
        scope,
        header,
        ["SOURCES", "HEADERS", "OBJECTIVE_SOURCES", "OBJECTIVE_HEADERS", "NO_PCH_SOURCES", "FORMS"] + extra_keys,
        indent,
        footer=footer,
    )


def write_defines(
    cm_fh: IO[str], scope: Scope, cmake_parameter: str, *, indent: int = 0, footer: str = ""
):
    defines = scope.expand("DEFINES")
    defines += [d[2:] for d in scope.expand("QMAKE_CXXFLAGS") if d.startswith("-D")]
    defines = [
        d.replace('=\\\\\\"$$PWD/\\\\\\"', '="${CMAKE_CURRENT_SOURCE_DIR}/"') for d in defines
    ]

    if "qml_debug" in scope.get("CONFIG"):
        defines.append("QT_QML_DEBUG")

    write_list(cm_fh, defines, cmake_parameter, indent, footer=footer)


def write_include_paths(
    cm_fh: IO[str], scope: Scope, cmake_parameter: str, *, indent: int = 0, footer: str = ""
):
    includes = [i.rstrip("/") or ("/") for i in scope.get_files("INCLUDEPATH")]

    write_list(cm_fh, includes, cmake_parameter, indent, footer=footer)


def write_compile_options(
    cm_fh: IO[str], scope: Scope, cmake_parameter: str, *, indent: int = 0, footer: str = ""
):
    compile_options = [d for d in scope.expand("QMAKE_CXXFLAGS") if not d.startswith("-D")]

    write_list(cm_fh, compile_options, cmake_parameter, indent, footer=footer)


def write_library_section(
    cm_fh: IO[str], scope: Scope, *, indent: int = 0, known_libraries: Set[str] = set()
):
    public_dependencies, private_dependencies = extract_cmake_libraries(
        scope, known_libraries=known_libraries
    )

    write_list(cm_fh, private_dependencies, "LIBRARIES", indent + 1)
    write_list(cm_fh, public_dependencies, "PUBLIC_LIBRARIES", indent + 1)


def write_autogen_section(cm_fh: IO[str], scope: Scope, *, indent: int = 0):
    forms = scope.get_files("FORMS")
    if forms:
        write_list(cm_fh, ["uic"], "ENABLE_AUTOGEN_TOOLS", indent)


def write_sources_section(cm_fh: IO[str], scope: Scope, *, indent: int = 0, known_libraries=set()):
    ind = spaces(indent)

    # mark RESOURCES as visited:
    scope.get("RESOURCES")

    write_all_source_file_lists(cm_fh, scope, "SOURCES", indent=indent + 1)

    write_source_file_list(cm_fh, scope, "DBUS_ADAPTOR_SOURCES", ["DBUS_ADAPTORS"], indent + 1)
    dbus_adaptor_flags = scope.expand("QDBUSXML2CPP_ADAPTOR_HEADER_FLAGS")
    if dbus_adaptor_flags:
        dbus_adaptor_flags_line = '" "'.join(dbus_adaptor_flags)
        cm_fh.write(f"{ind}    DBUS_ADAPTOR_FLAGS\n")
        cm_fh.write(f'{ind}        "{dbus_adaptor_flags_line}"\n')

    write_source_file_list(cm_fh, scope, "DBUS_INTERFACE_SOURCES", ["DBUS_INTERFACES"], indent + 1)
    dbus_interface_flags = scope.expand("QDBUSXML2CPP_INTERFACE_HEADER_FLAGS")
    if dbus_interface_flags:
        dbus_interface_flags_line = '" "'.join(dbus_interface_flags)
        cm_fh.write(f"{ind}    DBUS_INTERFACE_FLAGS\n")
        cm_fh.write(f'{ind}        "{dbus_interface_flags_line}"\n')

    write_defines(cm_fh, scope, "DEFINES", indent=indent + 1)

    write_include_paths(cm_fh, scope, "INCLUDE_DIRECTORIES", indent=indent + 1)

    write_library_section(cm_fh, scope, indent=indent, known_libraries=known_libraries)

    write_compile_options(cm_fh, scope, "COMPILE_OPTIONS", indent=indent + 1)

    write_autogen_section(cm_fh, scope, indent=indent + 1)

    link_options = scope.get("QMAKE_LFLAGS")
    if link_options:
        cm_fh.write(f"{ind}    LINK_OPTIONS\n")
        for lo in link_options:
            cm_fh.write(f'{ind}        "{lo}"\n')

    moc_options = scope.get("QMAKE_MOC_OPTIONS")
    if moc_options:
        cm_fh.write(f"{ind}    MOC_OPTIONS\n")
        for mo in moc_options:
            cm_fh.write(f'{ind}        "{mo}"\n')

    precompiled_header = scope.get("PRECOMPILED_HEADER")
    if precompiled_header:
        cm_fh.write(f"{ind}    PRECOMPILED_HEADER\n")
        for header in precompiled_header:
            cm_fh.write(f'{ind}        "{header}"\n')

    no_pch_sources = scope.get("NO_PCH_SOURCES")
    if no_pch_sources:
        cm_fh.write(f"{ind}    NO_PCH_SOURCES\n")
        for source in no_pch_sources:
            cm_fh.write(f'{ind}        "{source}"\n')


def is_simple_condition(condition: str) -> bool:
    return " " not in condition or (condition.startswith("NOT ") and " " not in condition[4:])


def write_ignored_keys(scope: Scope, indent: str) -> str:
    result = ""
    ignored_keys = scope.keys - scope.visited_keys
    for k in sorted(ignored_keys):
        if k in {
            "_INCLUDED",
            "TARGET",
            "QMAKE_DOCS",
            "QT_SOURCE_TREE",
            "QT_BUILD_TREE",
            "TRACEPOINT_PROVIDER",
            "PLUGIN_TYPE",
            "PLUGIN_CLASS_NAME",
            "CLASS_NAME",
            "MODULE_PLUGIN_TYPES",
        }:
            # All these keys are actually reported already
            continue
        values = scope.get(k)
        value_string = "<EMPTY>" if not values else '"' + '" "'.join(scope.get(k)) + '"'
        result += f"{indent}# {k} = {value_string}\n"

    if result:
        result = f"\n#### Keys ignored in scope {scope}:\n{result}"

    return result


def _iterate_expr_tree(expr, op, matches):
    assert expr.func == op
    keepers = ()
    for arg in expr.args:
        if arg in matches:
            matches = tuple(x for x in matches if x != arg)
        elif arg == op:
            (matches, extra_keepers) = _iterate_expr_tree(arg, op, matches)
            keepers = (*keepers, *extra_keepers)
        else:
            keepers = (*keepers, arg)
    return matches, keepers


def _simplify_expressions(expr, op, matches, replacement):
    for arg in expr.args:
        expr = expr.subs(arg, _simplify_expressions(arg, op, matches, replacement))

    if expr.func == op:
        (to_match, keepers) = tuple(_iterate_expr_tree(expr, op, matches))
        if len(to_match) == 0:
            # build expression with keepers and replacement:
            if keepers:
                start = replacement
                current_expr = None
                last_expr = keepers[-1]
                for repl_arg in keepers[:-1]:
                    current_expr = op(start, repl_arg)
                    start = current_expr
                top_expr = op(start, last_expr)
            else:
                top_expr = replacement

            expr = expr.subs(expr, top_expr)

    return expr


def _simplify_flavors_in_condition(base: str, flavors, expr):
    """ Simplify conditions based on the knownledge of which flavors
        belong to which OS. """
    base_expr = simplify_logic(base)
    false_expr = simplify_logic("false")
    for flavor in flavors:
        flavor_expr = simplify_logic(flavor)
        expr = _simplify_expressions(expr, And, (base_expr, flavor_expr), flavor_expr)
        expr = _simplify_expressions(expr, Or, (base_expr, flavor_expr), base_expr)
        expr = _simplify_expressions(expr, And, (Not(base_expr), flavor_expr), false_expr)
    return expr


def _simplify_os_families(expr, family_members, other_family_members):
    for family in family_members:
        for other in other_family_members:
            if other in family_members:
                continue  # skip those in the sub-family

            f_expr = simplify_logic(family)
            o_expr = simplify_logic(other)

            expr = _simplify_expressions(expr, And, (f_expr, Not(o_expr)), f_expr)
            expr = _simplify_expressions(expr, And, (Not(f_expr), o_expr), o_expr)
            expr = _simplify_expressions(expr, And, (f_expr, o_expr), simplify_logic("false"))
    return expr


def _recursive_simplify(expr):
    """ Simplify the expression as much as possible based on
        domain knowledge. """
    input_expr = expr

    # Simplify even further, based on domain knowledge:
    # windowses = ('WIN32', 'WINRT')
    apples = ("APPLE_OSX", "APPLE_UIKIT", "APPLE_IOS", "APPLE_TVOS", "APPLE_WATCHOS")
    bsds = ("FREEBSD", "OPENBSD", "NETBSD")
    androids = ("ANDROID", "ANDROID_EMBEDDED")
    unixes = (
        "APPLE",
        *apples,
        "BSD",
        *bsds,
        "LINUX",
        *androids,
        "HAIKU",
        "INTEGRITY",
        "VXWORKS",
        "QNX",
        "WASM",
    )

    unix_expr = simplify_logic("UNIX")
    win_expr = simplify_logic("WIN32")
    false_expr = simplify_logic("false")
    true_expr = simplify_logic("true")

    expr = expr.subs(Not(unix_expr), win_expr)  # NOT UNIX -> WIN32
    expr = expr.subs(Not(win_expr), unix_expr)  # NOT WIN32 -> UNIX

    # UNIX [OR foo ]OR WIN32 -> ON [OR foo]
    expr = _simplify_expressions(expr, Or, (unix_expr, win_expr), true_expr)
    # UNIX  [AND foo ]AND WIN32 -> OFF [AND foo]
    expr = _simplify_expressions(expr, And, (unix_expr, win_expr), false_expr)

    expr = _simplify_flavors_in_condition("WIN32", ("WINRT",), expr)
    expr = _simplify_flavors_in_condition("APPLE", apples, expr)
    expr = _simplify_flavors_in_condition("BSD", bsds, expr)
    expr = _simplify_flavors_in_condition("UNIX", unixes, expr)
    expr = _simplify_flavors_in_condition("ANDROID", ("ANDROID_EMBEDDED",), expr)

    # Simplify families of OSes against other families:
    expr = _simplify_os_families(expr, ("WIN32", "WINRT"), unixes)
    expr = _simplify_os_families(expr, androids, unixes)
    expr = _simplify_os_families(expr, ("BSD", *bsds), unixes)

    for family in ("HAIKU", "QNX", "INTEGRITY", "LINUX", "VXWORKS"):
        expr = _simplify_os_families(expr, (family,), unixes)

    # Now simplify further:
    expr = simplify_logic(expr)

    while expr != input_expr:
        input_expr = expr
        expr = _recursive_simplify(expr)

    return expr


def simplify_condition(condition: str) -> str:
    input_condition = condition.strip()

    # Map to sympy syntax:
    condition = " " + input_condition + " "
    condition = condition.replace("(", " ( ")
    condition = condition.replace(")", " ) ")

    tmp = ""
    while tmp != condition:
        tmp = condition

        condition = condition.replace(" NOT ", " ~ ")
        condition = condition.replace(" AND ", " & ")
        condition = condition.replace(" OR ", " | ")
        condition = condition.replace(" ON ", " true ")
        condition = condition.replace(" OFF ", " false ")
        # Replace dashes with a token
        condition = condition.replace("-", "_dash_")

    # SymPy chokes on expressions that contain two tokens one next to
    # the other delimited by a space, which are not an operation.
    # So a CMake condition like "TARGET Foo::Bar" fails the whole
    # expression simplifying process.
    # Turn these conditions into a single token so that SymPy can parse
    # the expression, and thus simplify it.
    # Do this by replacing and keeping a map of conditions to single
    # token symbols.
    # Support both target names without double colons, and with double
    # colons.
    pattern = re.compile(r"(TARGET [a-zA-Z]+(?:::[a-zA-Z]+)?)")
    target_symbol_mapping = {}
    all_target_conditions = re.findall(pattern, condition)
    for target_condition in all_target_conditions:
        # Replace spaces and colons with underscores.
        target_condition_symbol_name = re.sub("[ :]", "_", target_condition)
        target_symbol_mapping[target_condition_symbol_name] = target_condition
        condition = re.sub(target_condition, target_condition_symbol_name, condition)

    try:
        # Generate and simplify condition using sympy:
        condition_expr = simplify_logic(condition)
        condition = str(_recursive_simplify(condition_expr))

        # Restore the target conditions.
        for symbol_name in target_symbol_mapping:
            condition = re.sub(symbol_name, target_symbol_mapping[symbol_name], condition)

        # Map back to CMake syntax:
        condition = condition.replace("~", "NOT ")
        condition = condition.replace("&", "AND")
        condition = condition.replace("|", "OR")
        condition = condition.replace("True", "ON")
        condition = condition.replace("False", "OFF")
        condition = condition.replace("_dash_", "-")
    except (SympifyError, TypeError):
        # sympy did not like our input, so leave this condition alone:
        condition = input_condition

    return condition or "ON"


def recursive_evaluate_scope(
    scope: Scope, parent_condition: str = "", previous_condition: str = ""
) -> str:
    current_condition = scope.condition
    total_condition = current_condition
    if total_condition == "else":
        assert previous_condition, f"Else branch without previous condition in: {scope.file}"
        total_condition = f"NOT ({previous_condition})"
    if parent_condition:
        if not total_condition:
            total_condition = parent_condition
        else:
            total_condition = f"({parent_condition}) AND ({total_condition})"

    scope.total_condition = simplify_condition(total_condition)

    prev_condition = ""
    for c in scope.children:
        prev_condition = recursive_evaluate_scope(c, total_condition, prev_condition)

    return current_condition


def map_to_cmake_condition(condition: str = "") -> str:
    condition = condition.replace("QTDIR_build", "QT_BUILDING_QT")
    condition = re.sub(
        r"\bQT_ARCH___equals___([a-zA-Z_0-9]*)",
        r'(TEST_architecture_arch STREQUAL "\1")',
        condition or "",
    )
    condition = re.sub(
        r"\bQT_ARCH___contains___([a-zA-Z_0-9]*)",
        r'(TEST_architecture_arch STREQUAL "\1")',
        condition or "",
    )
    return condition


resource_file_expansion_counter = 0


def expand_resource_glob(cm_fh: IO[str], expression: str) -> str:
    global resource_file_expansion_counter
    r = expression.replace('"', "")

    cm_fh.write(
        dedent(
            f"""
        file(GLOB resource_glob_{resource_file_expansion_counter} RELATIVE "${{CMAKE_CURRENT_SOURCE_DIR}}" "{r}")
        foreach(file IN LISTS resource_glob_{resource_file_expansion_counter})
            set_source_files_properties("${{CMAKE_CURRENT_SOURCE_DIR}}/${{file}}" PROPERTIES QT_RESOURCE_ALIAS "${{file}}")
        endforeach()
        """
        )
    )

    expanded_var = f"${{resource_glob_{resource_file_expansion_counter}}}"
    resource_file_expansion_counter += 1
    return expanded_var


def write_resources(cm_fh: IO[str], target: str, scope: Scope, indent: int = 0, is_example=False):
    # vpath = scope.expand('VPATH')

    # Handle QRC files by turning them into add_qt_resource:
    resources = scope.get_files("RESOURCES")
    qtquickcompiler_skipped = scope.get_files("QTQUICK_COMPILER_SKIPPED_RESOURCES")
    qtquickcompiler_retained = scope.get_files("QTQUICK_COMPILER_RETAINED_RESOURCES")
    qrc_output = ""
    if resources:
        standalone_files: List[str] = []
        for r in resources:
            skip_qtquick_compiler = r in qtquickcompiler_skipped
            retain_qtquick_compiler = r in qtquickcompiler_retained
            if r.endswith(".qrc"):
                qrc_output += process_qrc_file(
                    target,
                    r,
                    scope.basedir,
                    scope.file_absolute_path,
                    skip_qtquick_compiler,
                    retain_qtquick_compiler,
                    is_example,
                )
            else:
                immediate_files = {f: "" for f in scope.get_files(f"{r}.files")}
                if immediate_files:
                    immediate_files_filtered = []
                    for f in immediate_files:
                        if "*" in f:
                            immediate_files_filtered.append(expand_resource_glob(cm_fh, f))
                        else:
                            immediate_files_filtered.append(f)
                    immediate_files = {f: "" for f in immediate_files_filtered}
                    immediate_prefix = scope.get(r + ".prefix")
                    if immediate_prefix:
                        immediate_prefix = immediate_prefix[0]
                    else:
                        immediate_prefix = "/"
                    immediate_base = scope.get(f"{r}.base")
                    immediate_lang = None
                    immediate_name = f"qmake_{r}"
                    qrc_output += write_add_qt_resource_call(
                        target,
                        immediate_name,
                        immediate_prefix,
                        immediate_base,
                        immediate_lang,
                        immediate_files,
                        skip_qtquick_compiler,
                        retain_qtquick_compiler,
                        is_example,
                    )
                else:
                    if "*" in r:
                        standalone_files.append(expand_resource_glob(cm_fh, r))
                    else:
                        # stadalone source file properties need to be set as they
                        # are parsed.
                        if skip_qtquick_compiler:
                            qrc_output += 'set_source_files_properties(f"{r}" PROPERTIES QT_SKIP_QUICKCOMPILER 1)\n\n'

                        if retain_qtquick_compiler:
                            qrc_output += 'set_source_files_properties(f"{r}" PROPERTIES QT_RETAIN_QUICKCOMPILER 1)\n\n'
                        standalone_files.append(r)

        if standalone_files:
            name = "qmake_immediate"
            prefix = "/"
            base = None
            lang = None
            files = {f: "" for f in standalone_files}
            skip_qtquick_compiler = False
            qrc_output += write_add_qt_resource_call(
                target,
                name,
                prefix,
                base,
                lang,
                files,
                skip_qtquick_compiler=False,
                retain_qtquick_compiler=False,
                is_example=is_example,
            )

    if qrc_output:
        cm_fh.write("\n# Resources:\n")
        for line in qrc_output.split("\n"):
            cm_fh.write(f"{' ' * indent}{line}\n")

def write_statecharts(cm_fh: IO[str], target: str, scope: Scope, indent: int = 0):
    sources = scope.get("STATECHARTS")
    if not sources:
        return
    cm_fh.write("\n# Statecharts:\n")
    cm_fh.write(f"qt6_add_statecharts({target}\n")
    indent += 1
    for f in sources:
        cm_fh.write(f"{spaces(indent)}{f}\n")
    cm_fh.write(")\n")

def write_extend_target(cm_fh: IO[str], target: str, scope: Scope, indent: int = 0):
    ind = spaces(indent)
    extend_qt_io_string = io.StringIO()
    write_sources_section(extend_qt_io_string, scope)
    extend_qt_string = extend_qt_io_string.getvalue()

    extend_scope = (
        f"\n{ind}extend_target({target} CONDITION"
        f" {map_to_cmake_condition(scope.total_condition)}\n"
        f"{extend_qt_string}{ind})\n"
    )

    if not extend_qt_string:
        extend_scope = ""  # Nothing to report, so don't!

    cm_fh.write(extend_scope)

    write_resources(cm_fh, target, scope, indent)


def flatten_scopes(scope: Scope) -> List[Scope]:
    result = [scope]  # type: List[Scope]
    for c in scope.children:
        result += flatten_scopes(c)
    return result


def merge_scopes(scopes: List[Scope]) -> List[Scope]:
    result = []  # type: List[Scope]

    # Merge scopes with their parents:
    known_scopes = {}  # type: Mapping[str, Scope]
    for scope in scopes:
        total_condition = scope.total_condition
        assert total_condition
        if total_condition == "OFF":
            # ignore this scope entirely!
            pass
        elif total_condition in known_scopes:
            known_scopes[total_condition].merge(scope)
        else:
            # Keep everything else:
            result.append(scope)
            known_scopes[total_condition] = scope

    return result


def write_simd_part(cm_fh: IO[str], target: str, scope: Scope, indent: int = 0):
    simd_options = [
        "sse2",
        "sse3",
        "ssse3",
        "sse4_1",
        "sse4_2",
        "aesni",
        "shani",
        "avx",
        "avx2",
        "avx512f",
        "avx512cd",
        "avx512er",
        "avx512pf",
        "avx512dq",
        "avx512bw",
        "avx512vl",
        "avx512ifma",
        "avx512vbmi",
        "f16c",
        "rdrnd",
        "neon",
        "mips_dsp",
        "mips_dspr2",
        "arch_haswell",
        "avx512common",
        "avx512core",
    ]
    for simd in simd_options:
        SIMD = simd.upper()
        write_source_file_list(
            cm_fh,
            scope,
            "SOURCES",
            [f"{SIMD}_HEADERS", f"{SIMD}_SOURCES", f"{SIMD}_C_SOURCES", f"{SIMD}_ASM"],
            indent,
            header=f"add_qt_simd_part({target} SIMD {simd}\n",
            footer=")\n\n",
        )


def write_android_part(cm_fh: IO[str], target: str, scope: Scope, indent: int = 0):
    keys = [
        "ANDROID_BUNDLED_JAR_DEPENDENCIES",
        "ANDROID_LIB_DEPENDENCIES",
        "ANDROID_JAR_DEPENDENCIES",
        "ANDROID_LIB_DEPENDENCY_REPLACEMENTS",
        "ANDROID_BUNDLED_FILES",
        "ANDROID_PERMISSIONS",
    ]

    has_no_values = True
    for key in keys:
        value = scope.get(key)
        if len(value) != 0:
            if has_no_values:
                if scope.condition:
                    cm_fh.write(f"\n{spaces(indent)}if(ANDROID AND ({scope.condition}))\n")
                else:
                    cm_fh.write(f"\n{spaces(indent)}if(ANDROID)\n")
                indent += 1
                has_no_values = False
            cm_fh.write(f"{spaces(indent)}set_property(TARGET {target} APPEND PROPERTY QT_{key}\n")
            write_list(cm_fh, value, "", indent + 1)
            cm_fh.write(f"{spaces(indent)})\n")
    indent -= 1

    if not has_no_values:
        cm_fh.write(f"{spaces(indent)}endif()\n")


def write_main_part(
    cm_fh: IO[str],
    name: str,
    typename: str,
    cmake_function: str,
    scope: Scope,
    *,
    extra_lines: List[str] = [],
    indent: int = 0,
    extra_keys: List[str],
    **kwargs: Any,
):
    # Evaluate total condition of all scopes:
    recursive_evaluate_scope(scope)

    is_qml_plugin = any("qml_plugin" == s for s in scope.get("_LOADED"))

    if "exceptions" in scope.get("CONFIG"):
        extra_lines.append("EXCEPTIONS")

    # Get a flat list of all scopes but the main one:
    scopes = flatten_scopes(scope)
    # total_scopes = len(scopes)
    # Merge scopes based on their conditions:
    scopes = merge_scopes(scopes)

    assert len(scopes)
    assert scopes[0].total_condition == "ON"

    scopes[0].reset_visited_keys()
    for k in extra_keys:
        scopes[0].get(k)

    # Now write out the scopes:
    write_header(cm_fh, name, typename, indent=indent)

    # collect all testdata and insert globbing commands
    has_test_data = False
    if typename == "Test":
        test_data = scope.expand("TESTDATA")
        if test_data:
            has_test_data = True
            cm_fh.write("# Collect test data\n")
            for data in test_data:
                if "*" in data:
                    cm_fh.write(
                        dedent(
                            f"""\
                        {spaces(indent)}file(GLOB_RECURSE test_data_glob
                        {spaces(indent+1)}RELATIVE ${{CMAKE_CURRENT_SOURCE_DIR}}
                        {spaces(indent+1)}{data})
                        """
                        )
                    )
                    cm_fh.write(f"{spaces(indent)}list(APPEND test_data ${{test_data_glob}})\n")
                else:
                    cm_fh.write(f'{spaces(indent)}list(APPEND test_data "{data}")\n')
            cm_fh.write("\n")

    # Check for DESTDIR override
    destdir = scope.get_string("DESTDIR")
    if destdir:
        if destdir.startswith("./") or destdir.startswith("../"):
            destdir = "${CMAKE_CURRENT_BINARY_DIR}/" + destdir
        extra_lines.append(f'OUTPUT_DIRECTORY "{destdir}"')

    cm_fh.write(f"{spaces(indent)}{cmake_function}({name}\n")
    for extra_line in extra_lines:
        cm_fh.write(f"{spaces(indent)}    {extra_line}\n")

    write_sources_section(cm_fh, scopes[0], indent=indent, **kwargs)

    if has_test_data:
        cm_fh.write(f"{spaces(indent)}    TESTDATA ${{test_data}}\n")
    # Footer:
    cm_fh.write(f"{spaces(indent)})\n")

    write_resources(cm_fh, name, scope, indent)

    write_statecharts(cm_fh, name, scope, indent)

    write_simd_part(cm_fh, name, scope, indent)

    write_android_part(cm_fh, name, scopes[0], indent)

    ignored_keys_report = write_ignored_keys(scopes[0], spaces(indent))
    if ignored_keys_report:
        cm_fh.write(ignored_keys_report)

    # Scopes:
    if len(scopes) == 1:
        return

    write_scope_header(cm_fh, indent=indent)

    for c in scopes[1:]:
        c.reset_visited_keys()
        write_android_part(cm_fh, name, c, indent=indent)
        write_extend_target(cm_fh, name, c, indent=indent)
        ignored_keys_report = write_ignored_keys(c, spaces(indent))
        if ignored_keys_report:
            cm_fh.write(ignored_keys_report)


def write_module(cm_fh: IO[str], scope: Scope, *, indent: int = 0) -> str:
    module_name = scope.TARGET
    if not module_name.startswith("Qt"):
        print(f"XXXXXX Module name {module_name} does not start with Qt!")

    extra = []

    # A module should be static when 'static' is in CONFIG
    # or when option(host_build) is used, as described in qt_module.prf.
    is_static = "static" in scope.get("CONFIG") or "host_build" in scope.get("_OPTION")

    if is_static:
        extra.append("STATIC")
    if "internal_module" in scope.get("CONFIG"):
        extra.append("INTERNAL_MODULE")
    if "no_module_headers" in scope.get("CONFIG"):
        extra.append("NO_MODULE_HEADERS")
    if "minimal_syncqt" in scope.get("CONFIG"):
        extra.append("NO_SYNC_QT")
    if "no_private_module" in scope.get("CONFIG"):
        extra.append("NO_PRIVATE_MODULE")
    if "header_module" in scope.get("CONFIG"):
        extra.append("HEADER_MODULE")

    module_config = scope.get("MODULE_CONFIG")
    if len(module_config):
        extra.append(f'QMAKE_MODULE_CONFIG {" ".join(module_config)}')

    module_plugin_types = scope.get_files("MODULE_PLUGIN_TYPES")
    if module_plugin_types:
        extra.append(f"PLUGIN_TYPES {' '.join(module_plugin_types)}")

    target_name = module_name[2:]
    write_main_part(
        cm_fh,
        target_name,
        "Module",
        "add_qt_module",
        scope,
        extra_lines=extra,
        indent=indent,
        known_libraries={},
        extra_keys=[],
    )

    if "qt_tracepoints" in scope.get("CONFIG"):
        tracepoints = scope.get_files("TRACEPOINT_PROVIDER")
        cm_fh.write(
            f"\n\n{spaces(indent)}qt_create_tracepoints({module_name[2:]} {' '.join(tracepoints)})\n"
        )

    return target_name


def write_tool(cm_fh: IO[str], scope: Scope, *, indent: int = 0) -> str:
    tool_name = scope.TARGET

    extra = ["BOOTSTRAP"] if "force_bootstrap" in scope.get("CONFIG") else []

    write_main_part(
        cm_fh,
        tool_name,
        "Tool",
        "add_qt_tool",
        scope,
        indent=indent,
        known_libraries={"Qt::Core"},
        extra_lines=extra,
        extra_keys=["CONFIG"],
    )

    return tool_name


def write_test(cm_fh: IO[str], scope: Scope, gui: bool = False, *, indent: int = 0) -> str:
    test_name = scope.TARGET
    assert test_name

    extra = ["GUI"] if gui else []
    libraries = {"Qt::Core", "Qt::Test"}

    if "qmltestcase" in scope.get("CONFIG"):
        libraries.add("Qt::QmlTest")
        extra.append("QMLTEST")
        importpath = scope.expand("IMPORTPATH")
        if importpath:
            extra.append("QML_IMPORTPATH")
            for path in importpath:
                extra.append(f'    "{path}"')

    write_main_part(
        cm_fh,
        test_name,
        "Test",
        "add_qt_test",
        scope,
        indent=indent,
        known_libraries=libraries,
        extra_lines=extra,
        extra_keys=[],
    )

    return test_name


def write_binary(cm_fh: IO[str], scope: Scope, gui: bool = False, *, indent: int = 0) -> None:
    binary_name = scope.TARGET
    assert binary_name

    is_qt_test_helper = "qt_test_helper" in scope.get("_LOADED")

    extra = ["GUI"] if gui and not is_qt_test_helper else []
    cmake_function_call = "add_qt_executable"

    if is_qt_test_helper:
        binary_name += "_helper"
        cmake_function_call = "add_qt_test_helper"

    target_path = scope.get_string("target.path")
    if target_path:
        target_path = target_path.replace("$$[QT_INSTALL_EXAMPLES]", "${INSTALL_EXAMPLESDIR}")
        extra.append(f'OUTPUT_DIRECTORY "{target_path}"')
        if "target" in scope.get("INSTALLS"):
            extra.append(f'INSTALL_DIRECTORY "{target_path}"')

    write_main_part(
        cm_fh,
        binary_name,
        "Binary",
        cmake_function_call,
        scope,
        extra_lines=extra,
        indent=indent,
        known_libraries={"Qt::Core"},
        extra_keys=["target.path", "INSTALLS"],
    )

    return binary_name


def write_find_package_section(
    cm_fh: IO[str], public_libs: List[str], private_libs: List[str], *, indent: int = 0
):
    packages = []  # type: List[LibraryMapping]
    all_libs = public_libs + private_libs

    for l in all_libs:
        info = find_library_info_for_target(l)
        if info and info not in packages:
            packages.append(info)

    # ind = spaces(indent)

    for p in packages:
        cm_fh.write(generate_find_package_info(p, use_qt_find_package=False, indent=indent))

    if packages:
        cm_fh.write("\n")


def write_example(
    cm_fh: IO[str], scope: Scope, gui: bool = False, *, indent: int = 0, is_plugin: bool = False
) -> str:
    binary_name = scope.TARGET
    assert binary_name

    cm_fh.write(
        "cmake_minimum_required(VERSION 3.14)\n"
        f"project({binary_name} LANGUAGES CXX)\n\n"
        "set(CMAKE_INCLUDE_CURRENT_DIR ON)\n\n"
        "set(CMAKE_AUTOMOC ON)\n"
        "set(CMAKE_AUTORCC ON)\n"
        "set(CMAKE_AUTOUIC ON)\n\n"
        'set(INSTALL_EXAMPLEDIR "examples")\n\n'
    )

    (public_libs, private_libs) = extract_cmake_libraries(scope)
    write_find_package_section(cm_fh, public_libs, private_libs, indent=indent)

    add_target = ""

    qmldir = None
    if is_plugin:
        if "qml" in scope.get("QT"):
            # Get the uri from the destination directory
            dest_dir = scope.expandString("DESTDIR")
            if not dest_dir:
                dest_dir = "${CMAKE_CURRENT_BINARY_DIR}"
            else:
                uri = os.path.basename(dest_dir)
                dest_dir = "${CMAKE_CURRENT_BINARY_DIR}/" + dest_dir

            add_target = f"qt6_add_qml_module({binary_name}\n"
            add_target += f'    OUTPUT_DIRECTORY "{dest_dir}"\n'
            add_target += "    VERSION 1.0\n"
            add_target += '    URI "{}"\n'.format(uri)

            qmldir_file_path = scope.get_files("qmldir.files")
            if qmldir_file_path:
                qmldir_file_path = os.path.join(os.getcwd(), qmldir_file_path[0])
            else:
                qmldir_file_path = os.path.join(os.getcwd(), "qmldir")

            if os.path.exists(qmldir_file_path):
                qml_dir = QmlDir()
                qml_dir.from_file(qmldir_file_path)
                if qml_dir.designer_supported:
                    add_target += "    DESIGNER_SUPPORTED\n"
                if len(qml_dir.classname) != 0:
                    add_target += f"    CLASSNAME {qml_dir.classname}\n"
                if len(qml_dir.imports) != 0:
                    add_target += "    IMPORTS\n{}".format("        \n".join(qml_dir.imports))
                if len(qml_dir.depends) != 0:
                    add_target += "    DEPENDENCIES\n"
                    for dep in qml_dir.depends:
                        add_target += f"        {dep[0]}/{dep[1]}\n"

            add_target += "    INSTALL_LOCATION ${INSTALL_EXAMPLEDIR}\n)\n\n"
            add_target += f"target_sources({binary_name} PRIVATE"
        else:
            add_target = f"add_library({binary_name} MODULE"

    else:
        add_target = f'add_{"qt_gui_" if gui else ""}executable({binary_name}'

    write_all_source_file_lists(cm_fh, scope, add_target, indent=0)

    cm_fh.write(")\n")

    write_include_paths(
        cm_fh, scope, f"target_include_directories({binary_name} PUBLIC", indent=0, footer=")"
    )
    write_defines(
        cm_fh,
        scope,
        "target_compile_definitions({} PUBLIC".format(binary_name),
        indent=0,
        footer=")",
    )
    write_list(
        cm_fh,
        private_libs,
        "",
        indent=indent,
        header="target_link_libraries({} PRIVATE\n".format(binary_name),
        footer=")",
    )
    write_list(
        cm_fh,
        public_libs,
        "",
        indent=indent,
        header="target_link_libraries({} PUBLIC\n".format(binary_name),
        footer=")",
    )
    write_compile_options(
        cm_fh, scope, "target_compile_options({}".format(binary_name), indent=0, footer=")"
    )

    write_resources(cm_fh, binary_name, scope, indent=indent, is_example=True)
    write_statecharts(cm_fh, binary_name, scope, indent=indent)

    if qmldir:
        write_qml_plugin_epilogue(cm_fh, binary_name, scope, qmldir, indent)

    cm_fh.write(
        "\ninstall(TARGETS {}\n".format(binary_name)
        + '    RUNTIME DESTINATION "${INSTALL_EXAMPLEDIR}"\n'
        + '    BUNDLE DESTINATION "${INSTALL_EXAMPLEDIR}"\n'
        + '    LIBRARY DESTINATION "${INSTALL_EXAMPLEDIR}"\n'
        + ")\n"
    )

    return binary_name


def write_plugin(cm_fh, scope, *, indent: int = 0) -> str:
    plugin_name = scope.TARGET
    assert plugin_name

    extra = []

    qmldir = None
    plugin_type = scope.get_string("PLUGIN_TYPE")
    is_qml_plugin = any("qml_plugin" == s for s in scope.get("_LOADED"))
    plugin_function_name = "add_qt_plugin"
    if plugin_type:
        extra.append(f"TYPE {plugin_type}")
    elif is_qml_plugin:
        plugin_function_name = "add_qml_module"
        qmldir = write_qml_plugin(cm_fh, plugin_name, scope, indent=indent, extra_lines=extra)

    plugin_class_name = scope.get_string("PLUGIN_CLASS_NAME")
    if plugin_class_name:
        extra.append("CLASS_NAME {}".format(plugin_class_name))

    write_main_part(
        cm_fh,
        plugin_name,
        "Plugin",
        plugin_function_name,
        scope,
        indent=indent,
        extra_lines=extra,
        known_libraries={},
        extra_keys=[],
    )

    if qmldir:
        write_qml_plugin_epilogue(cm_fh, plugin_name, scope, qmldir, indent)

    return plugin_name


def write_qml_plugin(
    cm_fh: IO[str],
    target: str,
    scope: Scope,
    *,
    extra_lines: typing.List[str] = [],
    indent: int = 0,
    **kwargs: typing.Any,
) -> QmlDir:
    # Collect other args if available
    indent += 2

    target_path = scope.get_string("TARGETPATH")
    if target_path:
        uri = target_path.replace("/", ".")
        import_name = scope.get_string("IMPORT_NAME")
        # Catch special cases such as foo.QtQuick.2.bar, which when converted
        # into a target path via cmake will result in foo/QtQuick/2/bar, which is
        # not what we want. So we supply the target path override.
        target_path_from_uri = uri.replace(".", "/")
        if target_path != target_path_from_uri:
            extra_lines.append(f'TARGET_PATH "{target_path}"')
        if import_name:
            extra_lines.append(f'URI "{import_name}"')
        else:
            uri = re.sub("\\.\\d+", "", uri)
            extra_lines.append(f'URI "{uri}"')

    import_version = scope.get_string("IMPORT_VERSION")
    if import_version:
        import_version = import_version.replace(
            "$$QT_MINOR_VERSION", "${CMAKE_PROJECT_VERSION_MINOR}"
        )
        extra_lines.append(f'VERSION "{import_version}"')

    plugindump_dep = scope.get_string("QML_PLUGINDUMP_DEPENDENCIES")

    if plugindump_dep:
        extra_lines.append(f'QML_PLUGINDUMP_DEPENDENCIES "{plugindump_dep}"')

    qmldir_file_path = os.path.join(os.getcwd(), "qmldir")
    if os.path.exists(qmldir_file_path):
        qml_dir = QmlDir()
        qml_dir.from_file(qmldir_file_path)
        if qml_dir.designer_supported:
            extra_lines.append("DESIGNER_SUPPORTED")
        if len(qml_dir.classname) != 0:
            extra_lines.append(f"CLASSNAME {qml_dir.classname}")
        if len(qml_dir.imports) != 0:
            extra_lines.append("IMPORTS\n        {}".format("\n        ".join(qml_dir.imports)))
        if len(qml_dir.depends) != 0:
            extra_lines.append("DEPENDENCIES")
            for dep in qml_dir.depends:
                extra_lines.append(f"    {dep[0]}/{dep[1]}")

        return qml_dir

    return None


def write_qml_plugin_epilogue(
    cm_fh: typing.IO[str], target: str, scope: Scope, qmldir: QmlDir, indent: int = 0
):

    qml_files = scope.get_files("QML_FILES", use_vpath=True)
    if qml_files:

        indent_0 = spaces(indent)
        indent_1 = spaces(indent + 1)
        # Quote file paths in case there are spaces.
        qml_files_quoted = ['"{}"'.format(f) for f in qml_files]

        cm_fh.write(
            "\n{}set(qml_files\n{}{}\n)\n".format(
                indent_0, indent_1, "\n{}".format(indent_1).join(qml_files_quoted)
            )
        )

        for qml_file in qml_files:
            if qml_file in qmldir.type_names:
                qmldir_file_info = qmldir.type_names[qml_file]
                cm_fh.write(
                    "{}set_source_files_properties({} PROPERTIES\n".format(indent_0, qml_file)
                )
                cm_fh.write(
                    '{}QT_QML_SOURCE_VERSION "{}"\n'.format(indent_1, qmldir_file_info.version)
                )
                # Only write typename if they are different, CMake will infer
                # the name by default
                if (
                    os.path.splitext(os.path.basename(qmldir_file_info.path))[0]
                    != qmldir_file_info.type_name
                ):
                    cm_fh.write(
                        "{}QT_QML_SOURCE_TYPENAME {}\n".format(indent_1, qmldir_file_info.type_name)
                    )
                cm_fh.write("{}QT_QML_SOURCE_INSTALL TRUE\n".format(indent_1))
                if qmldir_file_info.singleton:
                    cm_fh.write("{}QT_QML_SINGLETON_TYPE TRUE\n".format(indent_1))
                if qmldir_file_info.internal:
                    cm_fh.write("{}QT_QML_INTERNAL_TYPE TRUE\n".format(indent_1))
                cm_fh.write("{})\n".format(indent_0))

        cm_fh.write(
            "\n{}qt6_target_qml_files({}\n{}FILES\n{}${{qml_files}}\n)\n".format(
                indent_0, target, indent_1, spaces(indent + 2)
            )
        )


def handle_app_or_lib(
    scope: Scope, cm_fh: IO[str], *, indent: int = 0, is_example: bool = False
) -> None:
    assert scope.TEMPLATE in ("app", "lib")

    config = scope.get("CONFIG")
    is_lib = scope.TEMPLATE == "lib"
    is_qml_plugin = any("qml_plugin" == s for s in scope.get("_LOADED"))
    is_plugin = (
        any("qt_plugin" == s for s in scope.get("_LOADED")) or is_qml_plugin or "plugin" in config
    )
    target = ""
    gui = all(
        val not in config for val in ["console", "cmdline"]
    ) and "testlib" not in scope.expand("QT")

    if is_example:
        target = write_example(cm_fh, scope, gui, indent=indent, is_plugin=is_plugin)
    elif is_plugin:
        assert not is_example
        target = write_plugin(cm_fh, scope, indent=indent)
    elif is_lib or "qt_module" in scope.get("_LOADED"):
        assert not is_example
        target = write_module(cm_fh, scope, indent=indent)
    elif "qt_tool" in scope.get("_LOADED"):
        assert not is_example
        target = write_tool(cm_fh, scope, indent=indent)
    else:
        if "testcase" in config or "testlib" in config or "qmltestcase" in config:
            assert not is_example
            target = write_test(cm_fh, scope, gui, indent=indent)
        else:
            target = write_binary(cm_fh, scope, gui, indent=indent)

    # ind = spaces(indent)
    write_source_file_list(
        cm_fh, scope, "", ["QMAKE_DOCS"], indent, header=f"add_qt_docs({target}\n", footer=")\n"
    )


def handle_top_level_repo_project(scope: Scope, cm_fh: IO[str]):
    # qtdeclarative
    project_file_name = os.path.splitext(os.path.basename(scope.file_absolute_path))[0]

    # declarative
    file_name_without_qt_prefix = project_file_name[2:]

    # Qt::Declarative
    qt_lib = map_qt_library(file_name_without_qt_prefix)

    # Found a mapping, adjust name.
    if qt_lib != file_name_without_qt_prefix:
        # QtDeclarative
        qt_lib = re.sub(r":", r"", qt_lib)

        # Declarative
        qt_lib_no_prefix = qt_lib[2:]
    else:
        qt_lib += "_FIXME"
        qt_lib_no_prefix = qt_lib

    content = dedent(
        f"""\
                cmake_minimum_required(VERSION {cmake_version_string})

                project({qt_lib}
                    VERSION 6.0.0
                    DESCRIPTION "Qt {qt_lib_no_prefix} Libraries"
                    HOMEPAGE_URL "https://qt.io/"
                    LANGUAGES CXX C
                )

                find_package(Qt6 ${{PROJECT_VERSION}} CONFIG REQUIRED COMPONENTS BuildInternals Core SET_ME_TO_SOMETHING_USEFUL)
                find_package(Qt6 ${{PROJECT_VERSION}} CONFIG OPTIONAL_COMPONENTS SET_ME_TO_SOMETHING_USEFUL)
                qt_build_repo()
                """
    )

    cm_fh.write(f"{content}")


def find_top_level_repo_project_file(project_file_path: str = "") -> Optional[str]:
    qmake_conf_path = find_qmake_conf(project_file_path)
    qmake_dir = os.path.dirname(qmake_conf_path)

    # Hope to a programming god that there's only one .pro file at the
    # top level directory of repository.
    glob_result = glob.glob(os.path.join(qmake_dir, "*.pro"))
    if len(glob_result) > 0:
        return glob_result[0]
    return None


def handle_top_level_repo_tests_project(scope: Scope, cm_fh: IO[str]):
    top_level_project_path = find_top_level_repo_project_file(scope.file_absolute_path)
    if top_level_project_path:
        # qtdeclarative
        file_name = os.path.splitext(os.path.basename(top_level_project_path))[0]

        # declarative
        file_name_without_qt = file_name[2:]

        # Qt::Declarative
        qt_lib = map_qt_library(file_name_without_qt)

        # Found a mapping, adjust name.
        if qt_lib != file_name_without_qt:
            # QtDeclarative
            qt_lib = re.sub(r":", r"", qt_lib) + "Tests"
        else:
            qt_lib += "Tests_FIXME"
    else:
        qt_lib = "Tests_FIXME"

    content = dedent(
        f"""\
        if(NOT TARGET Qt::Test)
            cmake_minimum_required(VERSION {cmake_version_string})
            project({qt_lib} VERSION 6.0.0 LANGUAGES C CXX)
            find_package(Qt6 ${{PROJECT_VERSION}} REQUIRED COMPONENTS BuildInternals Core SET_ME_TO_SOMETHING_USEFUL)
            find_package(Qt6 ${{PROJECT_VERSION}} OPTIONAL_COMPONENTS SET_ME_TO_SOMETHING_USEFUL)
            qt_set_up_standalone_tests_build()
        endif()
        qt_build_tests()"""
    )

    cm_fh.write(f"{content}")


def cmakeify_scope(
    scope: Scope, cm_fh: IO[str], *, indent: int = 0, is_example: bool = False
) -> None:
    template = scope.TEMPLATE

    temp_buffer = io.StringIO()

    # Handle top level repo project in a special way.
    if is_top_level_repo_project(scope.file_absolute_path):
        handle_top_level_repo_project(scope, temp_buffer)
    # Same for top-level tests.
    elif is_top_level_repo_tests_project(scope.file_absolute_path):
        handle_top_level_repo_tests_project(scope, temp_buffer)
    elif template == "subdirs":
        handle_subdir(scope, temp_buffer, indent=indent, is_example=is_example)
    elif template in ("app", "lib"):
        handle_app_or_lib(scope, temp_buffer, indent=indent, is_example=is_example)
    else:
        print(f"    XXXX: {scope.file}: Template type {template} not yet supported.")

    buffer_value = temp_buffer.getvalue()

    if is_top_level_repo_examples_project(scope.file_absolute_path):
        # Wrap top level examples project with some commands which
        # are necessary to build examples as part of the overall
        # build.
        buffer_value = f"\nqt_examples_build_begin()\n\n{buffer_value}\nqt_examples_build_end()"

    cm_fh.write(buffer_value)


def generate_new_cmakelists(scope: Scope, *, is_example: bool = False) -> None:
    print("Generating CMakeLists.gen.txt")
    with open(scope.generated_cmake_lists_path, "w") as cm_fh:
        assert scope.file
        cm_fh.write(f"# Generated from {os.path.basename(scope.file)}.\n\n")

        is_example_heuristic = is_example_project(scope.file_absolute_path)
        final_is_example_decision = is_example or is_example_heuristic
        cmakeify_scope(scope, cm_fh, is_example=final_is_example_decision)


def do_include(scope: Scope, *, debug: bool = False) -> None:
    for c in scope.children:
        do_include(c)

    for include_file in scope.get_files("_INCLUDED", is_include=True):
        if not include_file:
            continue
        if not os.path.isfile(include_file):
            print(f"    XXXX: Failed to include {include_file}.")
            continue

        include_result = parseProFile(include_file, debug=debug)
        include_scope = Scope.FromDict(
            None, include_file, include_result.asDict().get("statements"), "", scope.basedir
        )  # This scope will be merged into scope!

        do_include(include_scope)

        scope.merge(include_scope)


def copy_generated_file_to_final_location(scope: Scope, keep_temporary_files=False) -> None:
    print(f"Copying {scope.generated_cmake_lists_path} to {scope.original_cmake_lists_path}")
    copyfile(scope.generated_cmake_lists_path, scope.original_cmake_lists_path)
    if not keep_temporary_files:
        os.remove(scope.generated_cmake_lists_path)


def should_convert_project(project_file_path: str = "") -> bool:
    qmake_conf_path = find_qmake_conf(project_file_path)
    qmake_conf_dir_path = os.path.dirname(qmake_conf_path)

    project_relative_path = os.path.relpath(project_file_path, qmake_conf_dir_path)

    # Skip cmake auto tests, they should not be converted.
    if project_relative_path.startswith("tests/auto/cmake"):
        return False

    # Skip qmake testdata projects.
    if project_relative_path.startswith("tests/auto/tools/qmake/testdata"):
        return False

    return True


def main() -> None:
    # Be sure of proper Python version
    assert sys.version_info >= (3, 7)

    args = _parse_commandline()

    debug_parsing = args.debug_parser or args.debug

    backup_current_dir = os.getcwd()

    for file in args.files:
        new_current_dir = os.path.dirname(file)
        file_relative_path = os.path.basename(file)
        if new_current_dir:
            os.chdir(new_current_dir)

        project_file_absolute_path = os.path.abspath(file_relative_path)
        if not should_convert_project(project_file_absolute_path):
            print(f'Skipping conversion of project: "{project_file_absolute_path}"')
            continue

        parseresult = parseProFile(file_relative_path, debug=debug_parsing)

        if args.debug_parse_result or args.debug:
            print("\n\n#### Parser result:")
            print(parseresult)
            print("\n#### End of parser result.\n")
        if args.debug_parse_dictionary or args.debug:
            print("\n\n####Parser result dictionary:")
            print(parseresult.asDict())
            print("\n#### End of parser result dictionary.\n")

        file_scope = Scope.FromDict(
            None, file_relative_path, parseresult.asDict().get("statements")
        )

        if args.debug_pro_structure or args.debug:
            print("\n\n#### .pro/.pri file structure:")
            file_scope.dump()
            print("\n#### End of .pro/.pri file structure.\n")

        do_include(file_scope, debug=debug_parsing)

        if args.debug_full_pro_structure or args.debug:
            print("\n\n#### Full .pro/.pri file structure:")
            file_scope.dump()
            print("\n#### End of full .pro/.pri file structure.\n")

        generate_new_cmakelists(file_scope, is_example=args.is_example)

        copy_generated_file = True
        if not args.skip_special_case_preservation:
            debug_special_case = args.debug_special_case_preservation or args.debug
            handler = SpecialCaseHandler(
                file_scope.original_cmake_lists_path,
                file_scope.generated_cmake_lists_path,
                file_scope.basedir,
                keep_temporary_files=args.keep_temporary_files,
                debug=debug_special_case,
            )

            copy_generated_file = handler.handle_special_cases()

        if copy_generated_file:
            copy_generated_file_to_final_location(
                file_scope, keep_temporary_files=args.keep_temporary_files
            )
        os.chdir(backup_current_dir)


if __name__ == "__main__":
    main()
