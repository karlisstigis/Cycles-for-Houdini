// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include "material_snapshot.h"
#include "material_stylesheets.h"
#include <functional>

namespace cycles_houdini {
struct GeometryRequest;
// Preparation-local memoization. No live geometry or callbacks survive prepare().
class NativeExternalBindings {
public:
  struct Mesh {
    std::shared_ptr<const MeshData> source, rendered;
    std::vector<NativeMaterialReference> references;
  };
  struct Context {
    double time;
    std::uint64_t &next_revision;
    std::set<std::string> &material_paths;
    std::set<std::string> &stylesheet_resources;
    const NativeMaterialPreparation &prepare_materials;
    std::function<void(SourceDiagnostic)> record_issue;
    std::function<std::shared_ptr<const MeshData>(const std::string &, const std::shared_ptr<const MeshData> &)>
        find_rendered;
    std::function<void(const std::string &, Mesh)> publish;
  };
  std::string resolve(const GeometryRequest &member, const GeometryRequest &owner,
                      const NativeMaterialAssignment &point_binding, const std::vector<UniformAttribute> &fields,
                      const Context &context);

private:
  std::map<std::string, std::string> variants_;
  std::map<std::pair<std::string, std::string>, NativeExternalMaterialStyle> styles_;
};
} // namespace cycles_houdini
