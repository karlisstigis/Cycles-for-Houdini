// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <string>
#include <UT/UT_Set.h>
class OP_Node;
class OP_Operator;
namespace cycles_houdini {
bool register_scene_definition(OP_Node &node, const std::string &dialog, unsigned inputs, unsigned outputs);
void clear_scene_definitions();
void collect_scene_definitions(const OP_Node &node, UT_Set<OP_Operator *> &active, bool descendants = true);
} // namespace cycles_houdini
