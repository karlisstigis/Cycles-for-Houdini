// SPDX-License-Identifier: Apache-2.0
#include "ies_ownership_contracts.h"
#include "../core/shader_graph.h"
#include "cycles_houdini/snapshot.h"
#include "scene/shader_nodes.h"
#include "util/md5.h"
#include <OpenImageIO/ustring.h>
#include <iostream>
#include <memory>
#include <stdexcept>
namespace cycles_houdini::harness {
void ies_ownership_contracts() {
  const auto require = [](bool condition, const char *message) {
    if (!condition)
      throw std::runtime_error(message);
  };
  std::size_t before{}, after{}, rounds = 64, expired = 0;
  std::string previous;
  for (std::size_t round = 0; round < rounds + 2; ++round) {
    const auto text = "IESNA:LM-63-2002\n[TEST] " + std::string(65536, 'x') + std::to_string(round) +
                      "\nTILT=NONE\n1 -1 1 2 1 1 2 0 0 0\n1 1 1\n0 180\n0\n1 1\n";
    ShaderGraphData data;
    data.nodes = {{"profile", "cycles/ies_light/1", {{"ies", text}}}, {"emission", "cycles/emission/1", {}}};
    data.links = {{{"profile", "fac"}, {"emission", "strength"}}};
    data.surface = {"emission", "emission"};
    ShaderGraphRequirements required;
    auto graph = prepare_shader_graph(data, required, {}, {}, ShaderContext::Light);
    ccl::IESLightNode *profile = nullptr;
    for (auto *node : graph->nodes)
      if ((profile = dynamic_cast<ccl::IESLightNode *>(node)))
        break;
    require(profile && profile->owned_ies && *profile->owned_ies == text, "IES text was not retained as owned storage");
    require(profile->get_ies().empty() && profile->get_filename().empty(),
            "Full IES text or a live filename reached the engine's string socket");
    ccl::MD5Hash hash;
    profile->hash(hash);
    const auto digest = hash.get_hex();
    require(previous.empty() || previous != digest, "Different owned IES resources alias during node hashing");
    previous = digest;
    std::weak_ptr<const ccl::string> weak = profile->owned_ies;
    ShaderGraphRequirements independent_required;
    auto independent = prepare_shader_graph(data, independent_required, {}, {}, ShaderContext::Light);
    ccl::IESLightNode *independently_prepared = nullptr;
    for (auto *node : independent->nodes)
      if ((independently_prepared = dynamic_cast<ccl::IESLightNode *>(node)))
        break;
    require(independently_prepared && independently_prepared->owned_ies == profile->owned_ies,
            "Separate emitters did not share unchanged validated IES text");
    auto cloned = ccl::make_unique<ccl::ShaderGraph>();
    auto *copy = static_cast<ccl::IESLightNode *>(profile->clone(cloned.get()));
    require(copy->owned_ies == profile->owned_ies,
            "IES compiler clone copied instead of sharing its immutable payload");
    graph.reset();
    require(!weak.expired(), "Original graph retirement invalidated a live IES clone");
    cloned.reset();
    require(!weak.expired(), "One emitter retirement invalidated another emitter's IES payload");
    independent.reset();
    require(weak.expired(), "IES text survived its final graph owner");
    ++expired;
    if (round == 1)
      before = OIIO::ustring::memory();
  }
  after = OIIO::ustring::memory();
  require(after <= before + 128 * 1024, "Editing IES text grew the permanent interned-string table");
  SceneUpdate initial;
  LightData light;
  light.key = "emitter";
  ShaderGraphData graph;
  graph.nodes = {{"color", "cycles/image_texture/1", {{"filename", std::string("op:/img/light/C")}}}};
  graph.surface = {"color", "color"};
  graph.image_time = 1.;
  light.graph = graph;
  initial.lights.push_back(light);
  auto resources = shader_resource_state({}, initial);
  require(texture_image_requests(*resources).size() == 1, "Light COP image omitted from the shared resource ledger");
  require(shader_resource_state(resources, {}) == resources, "Unchanged resource state was rebuilt");
  SceneUpdate removed;
  removed.deleted_lights.push_back(light.key);
  resources = shader_resource_state(resources, removed);
  require(texture_image_requests(*resources).empty(), "Deleted light retained its image reference");
  std::cout << "{\"result\":\"passed\",\"profiles\":" << rounds << ",\"payload_bytes\":65536,\"released\":" << expired
            << ",\"ustring_before\":" << before << ",\"ustring_after\":" << after
            << ",\"scope\":\"IES owned-text lifetime, clone sharing and content identity; not process/GPU memory "
               "certification\"}\n";
}
} // namespace cycles_houdini::harness
