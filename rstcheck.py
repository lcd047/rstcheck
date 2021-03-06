#!/usr/bin/env python

# Copyright (C) 2013-2014 Steven Myint
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""Checks code blocks in reStructuredText."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import io
import json
import locale
import os
import re
import subprocess
import sys
import tempfile

import docutils.core
import docutils.nodes
import docutils.parsers.rst
import docutils.utils
import docutils.writers


__version__ = '0.5.1'


def check(source, filename='<string>', report_level=1, ignore=None,
          warning_stream=sys.stderr):
    """Yield errors.

    Use lower report_level for noisier error output.

    Each yielded error is a tuple of the form:

        (line_number, message)

    Line numbers are indexed at 1 and are with respect to the full RST file.

    Each code block is checked asynchronously in a subprocess.

    """
    writer = CheckWriter(source, filename, ignore=ignore)

    docutils.core.publish_string(
        source, writer=writer,
        source_path=filename,
        settings_overrides={'report_level': report_level,
                            'warning_stream': warning_stream})

    for checker in writer.checkers:
        for error in checker():
            yield error


def _check_file(filename, report_level=1, ignore=None):
    """Yield errors."""
    if filename == '-':
        contents = sys.stdin.read()
    else:
        with open(filename) as input_file:
            contents = input_file.read()

    for error in check(contents,
                       filename=filename,
                       report_level=report_level,
                       ignore=ignore):
        yield error


def check_python(code):
    """Yield errors."""
    try:
        compile(code, '<string>', 'exec')
    except SyntaxError as exception:
        yield (int(exception.lineno), exception.msg)


def check_json(code):
    """Yield errors."""
    try:
        json.loads(code)
    except ValueError as exception:
        message = str(exception)
        line_number = 0

        found = re.search(r': line\s+([0-9]+)[^:]*$', message)
        if found:
            line_number = int(found.group(1))

        yield (int(line_number), message)


def check_rst(code, ignore):
    """Yield errors in nested RST code."""
    string_io = io.StringIO()
    filename = '<string>'

    for result in check(code,
                        filename=filename,
                        ignore=ignore,
                        warning_stream=string_io):
        yield result

    rst_errors = string_io.getvalue().strip()
    if rst_errors:
        for message in rst_errors.splitlines():
            try:
                yield parse_gcc_style_error_message(message,
                                                    filename=filename,
                                                    has_column=False)
            except ValueError:
                continue


class CodeBlockDirective(docutils.parsers.rst.Directive):

    """Code block directive."""

    has_content = True
    optional_arguments = 1

    def run(self):
        """Run directive."""
        try:
            language = self.arguments[0]
        except IndexError:
            language = ''
        code = '\n'.join(self.content)
        literal = docutils.nodes.literal_block(code, code)
        literal['classes'].append('code-block')
        literal['language'] = language
        return [literal]

docutils.parsers.rst.directives.register_directive('code-block',
                                                   CodeBlockDirective)
docutils.parsers.rst.directives.register_directive('sourcecode',
                                                   CodeBlockDirective)


class IgnoredDirective(docutils.parsers.rst.Directive):

    """Stub for unknown directives."""

    has_content = True

    def run(self):
        """Do nothing."""
        return []

# Ignore Sphinx directives.
for _directive in [
        'toctree',
        'todo',
        'versionadded',
        'versionchanged',
        'deprecated',
        'seealso',
        'centered',
        'hlist',
        'glossary',
        'productionlist']:
    docutils.parsers.rst.directives.register_directive(_directive,
                                                       IgnoredDirective)


def bash_checker(code):
    """Return checker."""
    run = run_in_subprocess(code, '.bash', ['bash', '-n'])

    def run_check():
        """Yield errors."""
        result = run()
        if result:
            (output, filename) = result
            prefix = filename + ': line '
            for line in output.splitlines():
                if not line.startswith(prefix):
                    continue
                message = line[len(prefix):]
                split_message = message.split(':', 1)
                yield (int(split_message[0]) - 1,
                       split_message[1].strip())
    return run_check


def c_checker(code):
    """Return checker."""
    return gcc_checker(code, '.c', [os.getenv('CC', 'gcc'), '-std=c99'])


def cpp_checker(code):
    """Return checker."""
    return gcc_checker(code, '.cpp', [os.getenv('CXX', 'g++'), '-std=c++0x'])


def gcc_checker(code, filename_suffix, arguments):
    """Return checker."""
    run = run_in_subprocess(code,
                            filename_suffix,
                            arguments + ['-pedantic', '-fsyntax-only'])

    def run_check():
        """Yield errors."""
        result = run()
        if result:
            (output, filename) = result
            for line in output.splitlines():
                try:
                    yield parse_gcc_style_error_message(line,
                                                        filename=filename)
                except ValueError:
                    continue

    return run_check


def parse_gcc_style_error_message(message, filename, has_column=True):
    """Parse GCC-style error message.

    Return (line_number, message). Raise ValueError if message cannot be
    parsed.

    """
    colons = 2 if has_column else 1
    prefix = filename + ':'
    if not message.startswith(prefix):
        raise ValueError()
    message = message[len(prefix):]
    split_message = message.split(':', colons)
    line_number = int(split_message[0])
    return (line_number,
            split_message[colons].strip())


def run_in_subprocess(code, filename_suffix, arguments):
    """Return None on success."""
    temporary_file = tempfile.NamedTemporaryFile(mode='w',
                                                 suffix=filename_suffix)
    temporary_file.write(code)
    temporary_file.flush()

    process = subprocess.Popen(arguments + [temporary_file.name],
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)

    def run():
        """Yield errors."""
        raw_result = process.communicate()
        if process.returncode != 0:
            return (raw_result[1].decode(locale.getpreferredencoding()),
                    temporary_file.name)

    return run


class CheckTranslator(docutils.nodes.NodeVisitor):

    """Visits code blocks and checks for syntax errors in code."""

    def __init__(self, document, contents, filename, ignore):
        docutils.nodes.NodeVisitor.__init__(self, document)
        self.checkers = []
        self.contents = contents
        self.filename = filename
        self.ignore = ignore or []

    def visit_literal_block(self, node):
        """Check syntax of code block."""
        if 'code-block' not in node['classes']:
            return

        language = node.get('language')
        if language in self.ignore:
            return

        checker = {
            'bash': bash_checker,
            'c': c_checker,
            'cpp': cpp_checker,
            'json': lambda source: lambda: check_json(source),
            'python': lambda source: lambda: check_python(source),
            'rst': lambda source: lambda: check_rst(source, ignore=self.ignore)
        }.get(language)

        if checker:
            run = checker(node.rawsource)

            def run_check():
                """Yield errors."""
                all_results = run()
                if all_results is not None:
                    if all_results:
                        for result in all_results:
                            error_offset = result[0] - 1

                            yield (
                                beginning_of_code_block(node, self.contents) +
                                error_offset,
                                '({}) {}'.format(language, result[1]))
                    else:
                        yield (self.filename, 0, 'unknown error')
            self.checkers.append(run_check)

        raise docutils.nodes.SkipNode

    def unknown_visit(self, node):
        """Ignore."""

    def unknown_departure(self, node):
        """Ignore."""


def beginning_of_code_block(node, full_contents):
    """Return line number of beginning of code block."""
    lines = full_contents.splitlines()
    line_number = node.line
    code_block_length = len(node.rawsource.splitlines())

    try:
        # Case where there are no extra spaces.
        if lines[line_number - 1].strip():
            return line_number - code_block_length + 1
    except IndexError:
        pass

    # The offsets are wrong if the RST text has multiple blank lines after the
    # code block. This is a workaround.
    for line_number in range(node.line, 1, -1):
        if lines[line_number - 2].strip():
            break

    return line_number - code_block_length


class CheckWriter(docutils.writers.Writer):

    """Runs CheckTranslator on code blocks."""

    def __init__(self, contents, filename, ignore):
        docutils.writers.Writer.__init__(self)
        self.checkers = []
        self.contents = contents
        self.filename = filename
        self.ignore = ignore

    def translate(self):
        """Run CheckTranslator."""
        visitor = CheckTranslator(self.document,
                                  contents=self.contents,
                                  filename=self.filename,
                                  ignore=self.ignore)
        self.document.walkabout(visitor)
        self.checkers += visitor.checkers


def main():
    """Return 0 on success."""
    parser = argparse.ArgumentParser(description=__doc__, prog='rstcheck')
    parser.add_argument('files', nargs='+',
                        help='files to check')
    parser.add_argument('--report', type=int, metavar='level', default=1,
                        help='report system messages at or higher than level; '
                             '1 info, 2 warning, 3 error, 4 severe, 5 none '
                             '(default: %(default)s)')
    parser.add_argument('--ignore', metavar='language', default='',
                        help='comma-separated list of languages to ignore')
    parser.add_argument('--version', action='version',
                        version='%(prog)s ' + __version__)
    args = parser.parse_args()

    status = 0
    for filename in args.files:
        try:
            for error in _check_file(filename,
                                     report_level=args.report,
                                     ignore=args.ignore.split(',')):
                print('{}:{}: (ERROR/3) {}'.format(filename,
                                                   error[0],
                                                   error[1]),
                      file=sys.stderr)
                status = 1
        except docutils.utils.SystemMessage:
            # docutils already prints a message to standard error.
            status = 1

    return status


if __name__ == '__main__':
    sys.exit(main())
