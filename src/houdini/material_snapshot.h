// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "material_assignment.h"
#include "cycles_houdini/session.h"
#include <functional>

namespace cycles_houdini {
struct NativeMaterialPreparation {
  std::function<std::set<std::string>(const NativeMaterialLookup &)> prepare;
  std::function<bool(const std::string &)> has_displacement;
  std::function<bool(const std::string &, const std::string &, std::size_t)> same_displacement;
  std::function<std::string(const std::string &, std::size_t)> displacement_token;
  std::function<bool(const std::string &, bool)> has_pointiness;
  std::function<std::vector<SourceDiagnostic>(const std::string &)> diagnostics;
  explicit operator bool() const { return bool(prepare); }
  std::set<std::string> operator()(const NativeMaterialLookup &candidates) const { return prepare(candidates); }
  bool pointiness(const NativeMaterialLookup &candidates) const {
    if (!has_pointiness)
      return false;
    for (const auto &[assignment, reference] : candidates)
      if (has_pointiness(reference.key, candidates.displacement_only))
        return true;
    if (candidates.elements)
      for (const auto &reference : candidates.elements->references)
        if (has_pointiness(reference->key, candidates.displacement_only))
          return true;
    return false;
  }
};
using NativeMaterialGraphResolver = std::function<MaterialData(const NativeMaterialReference &)>;
// Acquisition owns evaluated values or deferred diagnostics. Neither the
// resolver nor any host node/Python object is retained in this table.
using NativeMaterialGraphTable = std::map<std::string, MaterialData>;
class NativeMaterialSourceError : public std::invalid_argument {
public:
  using std::invalid_argument::invalid_argument;
};
void prepare_native_material_graphs(NativeMaterialGraphTable &graphs, const NativeMaterialLookup &candidates,
                                    const NativeMaterialGraphResolver &resolve);
// Pure selection after conversion discovers effective bindings. Candidate
// graphs that were not used cannot fail an otherwise valid scene.
std::vector<MaterialData> select_native_material_graphs(const NativeMaterialGraphTable &graphs,
                                                        const std::vector<NativeMaterialReference> &references);
} // namespace cycles_houdini
