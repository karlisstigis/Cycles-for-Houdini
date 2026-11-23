// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/shader.h"
#include "cycles_houdini/diagnostic.h"
namespace cycles_houdini {
// Authored tree-inlining adapters, rather than reflected Cycles node types.
std::vector<ShaderDefinition> shader_switch_definitions();
bool shader_switch_definition(const std::string &id);
// Uses upstream ConstantFolder for constant numeric utility subgraphs. No scene,
// texture, host node or sampled shader field is evaluated by this query.
std::optional<ShaderValue> shader_constant_value(const ShaderGraphData &, const ShaderEndpoint &);
ShaderGraphData lower_shader_switches(const ShaderGraphData &source,
                                      std::vector<SourceDiagnostic> *diagnostics = nullptr);
} // namespace cycles_houdini
