// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <string>
#include <map>
#include <vector>
#include <UT/UT_Color.h>
class OP_Operator;
class VOP_Node;
namespace cycles_houdini {
bool is_native_shader_operator(const OP_Operator &op);
bool is_cycles_ramp_operator(const OP_Operator &op);
// Blender 5.2 factory categories/palette; user-authored node colors stay owned
// by Houdini. These are operator defaults, not colors forced on every capture.
UT_Color native_shader_node_color(const std::string &category, const std::string &engine_type = {});
std::string native_shader_bypass_input(VOP_Node &node, const std::string &output);
bool native_shader_input_active(VOP_Node &node, const std::string &socket, double time);
std::map<std::string, bool> native_shader_input_activity(VOP_Node &node, double time,
                                                         const std::map<std::string, std::string> &values = {});
std::vector<std::string> native_shader_input_controls(VOP_Node &node);
} // namespace cycles_houdini
