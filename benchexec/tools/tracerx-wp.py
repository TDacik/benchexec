# This file is part of BenchExec, a framework for reliable benchmarking:
# https://github.com/sosy-lab/benchexec
#
# SPDX-FileCopyrightText: 2007-2020 Dirk Beyer <https://www.sosy-lab.org>
#
# SPDX-License-Identifier: Apache-2.0

import benchexec.tools.tracerx as tracerx


class Tool(tracerx.Tool):
    """
    Tool info for TracerX-WP (https://www.comp.nus.edu.sg/~tracerx/).
    """

    def executable(self, tool_locator):
        return tool_locator.find_executable("tracerx-wp", subdir="bin")

    def name(self):
        return "TracerX-WP"
