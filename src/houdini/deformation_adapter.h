// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "scene_adapter.h"
#include <variant>
#include <stdexcept>
class SOP_Node;
class GU_Detail;
namespace cycles_houdini {
class NativeDeformationSourceError : public std::invalid_argument {
public:
  using std::invalid_argument::invalid_argument;
};
// Cooperative check at shutter cook boundaries within a Houdini long operation.
class MotionCaptureInterrupted : public std::runtime_error {
public:
  MotionCaptureInterrupted() : std::runtime_error("Motion blur scene acquisition cancelled") {}
};
void check_shutter_interrupt();

struct NativeDeformation {
  std::variant<MeshData, PointData, CurveData> data;
  std::vector<std::int64_t> identifiers;
  std::vector<NativeMaterialReference> material_references;
  std::vector<SourceDiagnostic> source_diagnostics;
  std::set<std::string> shader_attributes;
  std::uint64_t primitive_count{}, index_ids{};
  std::string motion_warning;
};
std::vector<std::int64_t> deformation_identifiers(const GU_Detail &source, SOP_Node &sop,
                                                  const GeometryRequest &request, const NativeMotionWindow &window,
                                                  NativeGeometryKind kind);
// Same identifiers for a root source; identical to the SOP overload for ordinary requests.
std::vector<std::int64_t> deformation_identifiers(const GU_Detail &source, NativeRootSource &sop,
                                                  const GeometryRequest &request, const NativeMotionWindow &window,
                                                  NativeGeometryKind kind);
// Public edit versions of a SOP network (inputs, references, subnet contents).
void native_source_dependencies(std::vector<std::int64_t> &result, SOP_Node &root);
// All shutter details are already acquired. Their payload IDs, rather than an
// outer SOP's cook/edit count, identify whether prototype conversion is needed.
std::vector<std::int64_t> deformation_payload_identifiers(const GU_Detail &source, const GeometryRequest &request,
                                                          const NativeMotionWindow &window, NativeGeometryKind kind);
struct PreparedDeformation;
struct NativeDeformationSample {
  std::shared_ptr<const PreservedDetail> detail;
  GeometryRequest request;
  std::shared_ptr<const NativeGeometrySelection>
      selection; // Frozen classification of this owned sample, when available.
};
// Pure assembly of immutable external mesh samples, in chronological shutter
// order. Index/connectivity correspondence is required; failures retain the
// requested-frame mesh and emit a diagnostic rather than inventing motion.
MeshData assemble_external_mesh_motion(const MeshData &nominal,
                                       const std::vector<std::shared_ptr<const MeshData>> &samples,
                                       const NativeMotionWindow &window, const std::string &source,
                                       std::vector<SourceDiagnostic> &diagnostics);
// Pure assembly of already acquired packed contents, safe off the host thread.
// Samples own their complete inputs/frozen bindings; never resolve a primitive
// or evaluate an OP here. Caller retains exclusive ownership during assembly.
std::shared_ptr<const PreparedDeformation>
prepare_deformation_samples(NativeGeometryKind kind, NativeDeformationSample nominal,
                            std::vector<NativeDeformationSample> samples, std::vector<std::int64_t> identifiers,
                            const NativeMotionWindow &window, std::string warning = {});
// Host-only acquisition. The opaque result owns private geometry samples,
// frozen material identities and correspondence identifiers, never SOP pointers.
std::shared_ptr<const PreparedDeformation>
prepare_deformation_set(SOP_Node &sop, const std::map<NativeGeometryKind, GeometryRequest> &requests,
                        const NativeMotionWindow &window, ExtractionStats &stats,
                        const NativeMaterialPreparation &prepare_materials = {});
// Pure conversion of retained samples. Safe to invoke off the host thread;
// stats and the returned arrays belong exclusively to this invocation.
std::map<NativeGeometryKind, NativeDeformation> convert_deformation_set(const PreparedDeformation &prepared,
                                                                        std::uint64_t revision, ExtractionStats &stats,
                                                                        bool recover_source = false);
// Ordered wrapper used by current capture. Scheduling remains a caller policy.
std::map<NativeGeometryKind, NativeDeformation>
capture_deformation_set(SOP_Node &sop, const std::map<NativeGeometryKind, GeometryRequest> &requests,
                        std::uint64_t revision, const NativeMotionWindow &window, ExtractionStats &stats,
                        const NativeMaterialPreparation &prepare_materials = {});
} // namespace cycles_houdini
