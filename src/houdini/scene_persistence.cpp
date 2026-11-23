// SPDX-License-Identifier: Apache-2.0
#include "scene_persistence.h"
#include "material_vop.h"
#include <FS/FS_IndexFile.h>
#include <OP/OP_Director.h>
#include <OP/OP_Operator.h>
#include <UT/UT_Thread.h>
#include <VOP/VOP_Node.h>
#include <limits>
#include <map>
#include <stdexcept>

namespace cycles_houdini {
namespace {
struct Schema {
  std::string dialog;
  unsigned inputs{}, outputs{};
};
std::map<OP_Operator *, Schema> schemas;
void host_thread() {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Scene definitions require Houdini's host thread");
}
} // namespace
bool register_scene_definition(OP_Node &node, const std::string &dialog, unsigned inputs, unsigned outputs) {
  host_thread();
  auto *op = node.getOperator();
  if (!op || op->getIsDummyDefinition() || !is_native_shader_operator(*op))
    throw std::invalid_argument("Only this runtime's native Cycles operators can author scene definitions");
  if (dialog.size() > std::numeric_limits<int>::max())
    throw std::invalid_argument("Operator dialog script is too large");
  const auto old = schemas.find(op);
  const bool changed = old == schemas.end() || old->second.dialog != dialog || old->second.inputs != inputs ||
                       old->second.outputs != outputs;
  auto definition = op->getOTLDefinition();
  definition.setName(op->getName());
  definition.setOpTableName(VOP_TABLE_NAME);
  definition.setLabel(op->getEnglish());
  const auto *network = dynamic_cast<const OP_Network *>(&node);
  definition.setIsSubnet(network && network->getChildType());
  definition.setIsDummyDefinition(true);
  definition.setMinInputs(0);
  definition.setMaxInputs(inputs);
  definition.setMaxOutputs(outputs);
  auto extra = definition.getExtraInfoBuffer();
  if (op->getOpSpecificData())
    op->getOpSpecificData()->updateExtraInfoBuffer(extra);
  definition.setExtraInfoBuffer(extra);
  FS_IndexFile sections;
  sections.addSection("DialogScript", dialog.data(), static_cast<int>(dialog.size()));
  // Register reduced fallback data only. Installing an HDA under a live DSO
  // type can replace its instances, even with create_op_type=false.
  // Re-publish on every save: merging a HIP or loading a network fragment can
  // replace the manager's dummy without changing our live operator/schema.
  OPgetDirector()->getOTLManager().setDummyDefinition(definition, &sections);
  schemas[op] = Schema{dialog, inputs, outputs};
  return changed;
}
void clear_scene_definitions() {
  host_thread();
  schemas.clear();
}
void collect_scene_definitions(const OP_Node &node, UT_Set<OP_Operator *> &active, bool descendants) {
  if (is_native_shader_operator(*node.getOperator()) && schemas.contains(node.getOperator()))
    active.insert(node.getOperator());
  // Subnet connectors retain SideFX's vtable. A native builder includes
  // registered descendants explicitly instead of changing those classes.
  if (!descendants)
    return;
  for (int index = 0; index < node.getNchildren(); ++index)
    if (const auto *child = node.getChild(index))
      collect_scene_definitions(*child, active);
}
} // namespace cycles_houdini
