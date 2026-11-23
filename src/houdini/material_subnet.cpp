// SPDX-License-Identifier: Apache-2.0
// Dynamic group interfaces remain host-native even without the renderer.
// Shading is still evaluated by our owned, flattened Cycles graph compiler.
#include "material_subnet.h"
#include <OP/OP_Operator.h>

namespace cycles_houdini {
bool is_cycles_subnet_operator(const OP_Operator &op) {
  const auto name = op.getName();
  return name == "subnet" || name == "subnetconnector";
}
} // namespace cycles_houdini
