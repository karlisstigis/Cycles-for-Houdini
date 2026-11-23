# SPDX-License-Identifier: Apache-2.0
"""Houdini help generation must preserve C++ adjacent literals after formatting."""

import runpy

import pytest

from support.paths import ROOT

pytestmark = [pytest.mark.cpu, pytest.mark.pure]


def test_adjacent_summaries_and_color_ramp_escape():
    parse = runpy.run_path(str(ROOT / "scripts/shader_help_metadata.py"))["shader_help_metadata"]
    table = r"""
      {"node", {"A label", "Category", "First " "and second."}}
      void register_color_ramp(OP_OperatorTable &table) {
        data->help =
          "Quoted \"text\" " "and a second line.";
      }
    """
    data = parse(table)
    assert data["node"] == ("A label", "Category", "First and second.")
    assert data["color_ramp"] == ("Color Ramp", "Converter", 'Quoted "text" and a second line.')
