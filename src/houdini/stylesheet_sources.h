// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <string>
#include <vector>

namespace cycles_houdini {
struct NativeStyleSources {
  bool safe{true};
  bool scripted{};
  std::vector<std::string> resources, errors;
};
// Acquisition only. Native Houdini still owns import priority and evaluation.
// These routines watch registered names and ordinary files, including missing
// sources, without retaining a host object or modifying the registered map.
NativeStyleSources stylesheet_sources(const std::string &);
std::vector<std::string> stylesheet_source_signature(const std::vector<std::string> &);
struct NativeObjectStyles : NativeStyleSources {
  std::string json;
};
NativeObjectStyles object_stylesheet(const std::string &path, const std::vector<std::string> &layers,
                                     const std::string &own, const std::string &tags, double time);
} // namespace cycles_houdini
