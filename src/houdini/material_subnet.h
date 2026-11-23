// SPDX-License-Identifier: Apache-2.0
#pragma once
class OP_Node;
class OP_Operator;
class OP_OperatorTable;
namespace cycles_houdini {
void set_native_shader_identity(OP_Node &node);
bool is_cycles_subnet_operator(const OP_Operator &op);
} // namespace cycles_houdini
