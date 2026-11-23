// SPDX-License-Identifier: Apache-2.0
#include "material_snapshot.h"
#include <algorithm>
#include <stdexcept>
#include <UT/UT_Thread.h>

namespace cycles_houdini {
namespace {
MaterialData diagnostic_material(const NativeMaterialReference &reference, const SourceDiagnostic &issue) {
  MaterialData material;
  material.key = reference.key;
  material.cryptomatte_name = reference.path;
  material.kind = reference.volume ? MaterialKind::PrincipledVolume : MaterialKind::DiagnosticSurface;
  material.color = {1, 0, 1};
  material.source_diagnostics = {issue};
  if (reference.volume)
    material.source_diagnostics.front().action += "; using density-based ray marching with no emission";
  return material;
}
MaterialData select_material(const NativeMaterialGraphTable &graphs, const NativeMaterialReference &reference) {
  if (const auto found = graphs.find(reference.key); found != graphs.end())
    return found->second;
  if (!reference.timeline)
    throw std::logic_error("Native material graph was not frozen: " + reference.key);
  const auto &timeline = *reference.timeline;
  if (!timeline.nominal || timeline.samples.size() < 3 || timeline.samples.size() > 129 ||
      timeline.samples.size() % 2 == 0)
    throw std::logic_error("Malformed owned material shutter recipe");
  auto result = select_material(graphs, *timeline.nominal);
  result.key = reference.key;
  for (auto &issue : result.source_diagnostics)
    if (issue.resource == timeline.nominal->key)
      issue.resource = reference.key;
  if (!result.graph || !result.graph->displacement || result.graph->displacement_method == DisplacementMethod::Bump)
    return result;
  result.displacement_samples.clear();
  for (std::size_t step = 0; step < timeline.samples.size(); ++step) {
    const auto &binding = timeline.samples[step];
    if (!binding || binding->timeline)
      throw std::logic_error("Nested or missing material shutter reference");
    const auto source = select_material(graphs, *binding);
    const ShaderGraphData *sample = source.graph ? &*source.graph : nullptr;
    if (!source.displacement_samples.empty()) {
      if (source.displacement_samples.size() != timeline.samples.size())
        throw std::logic_error("Material shutter recipe/sample count mismatch");
      sample = &source.displacement_samples[step];
    }
    if (!sample || !sample->displacement || sample->displacement_method != result.graph->displacement_method) {
      result.displacement_samples.clear();
      result.graph->displacement_time_dependent = false;
      const auto detail =
          source.source_diagnostics.empty() ? std::string{} : "; " + source.source_diagnostics.front().message;
      result.source_diagnostics.push_back(
          {"motion.displacement_binding", binding->path, reference.key,
           "A material assignment has no compatible true displacement at shutter sample " + std::to_string(step) +
               detail,
           "Using requested-frame displacement; retaining independent geometry and transform motion"});
      return result;
    }
    for (auto issue : source.source_diagnostics) {
      issue.resource = reference.key;
      if (std::find(result.source_diagnostics.begin(), result.source_diagnostics.end(), issue) ==
          result.source_diagnostics.end())
        result.source_diagnostics.push_back(std::move(issue));
    }
    auto owned = displacement_snapshot(*sample);
    owned.displacement_time_dependent = false;
    result.displacement_samples.push_back(std::move(owned));
  }
  return result;
}
void prepare_material(NativeMaterialGraphTable &graphs, const NativeMaterialReference &reference,
                      const NativeMaterialGraphResolver &resolve) {
  if (graphs.contains(reference.key))
    return;
  if (reference.timeline) {
    prepare_material(graphs, *reference.timeline->nominal, resolve);
    for (const auto &sample : reference.timeline->samples)
      prepare_material(graphs, *sample, resolve);
    graphs.emplace(reference.key, select_material(graphs, reference));
    return;
  }
  if (reference.diagnostic) {
    graphs.emplace(reference.key, diagnostic_material(reference, *reference.diagnostic));
    return;
  }
  if (!reference.session_id && !reference.key.starts_with("native-material:"))
    throw std::logic_error("Owned uniform material was not frozen: " + reference.key);
  try {
    auto material = resolve(reference);
    if (material.key != reference.key || material.kind != MaterialKind::Graph || !material.graph)
      throw std::invalid_argument("Material resolver must return exactly one graph for each referenced native key");
    graphs.emplace(reference.key, std::move(material));
  } catch (const NativeMaterialSourceError &error) {
    graphs.emplace(reference.key, diagnostic_material(
                                      reference, {"material.invalid_graph", reference.path, reference.key, error.what(),
                                                  "Rendering assigned geometry with a diagnostic material"}));
  }
}
} // namespace
void prepare_native_material_graphs(NativeMaterialGraphTable &graphs, const NativeMaterialLookup &candidates,
                                    const NativeMaterialGraphResolver &resolve) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Material graph preparation requires the host thread");
  for (const auto &[path, candidate] : candidates)
    prepare_material(graphs, candidate, resolve);
  if (candidates.elements)
    for (const auto &reference : candidates.elements->references) {
      if (!reference)
        throw std::logic_error("Missing owned sampled material reference");
      prepare_material(graphs, *reference, resolve);
    }
}
std::vector<MaterialData> select_native_material_graphs(const NativeMaterialGraphTable &graphs,
                                                        const std::vector<NativeMaterialReference> &references) {
  std::vector<MaterialData> result;
  std::set<std::string> selected;
  for (const auto &reference : references) {
    if (!selected.insert(reference.key).second)
      continue;
    result.push_back(select_material(graphs, reference));
  }
  return result;
}
} // namespace cycles_houdini
