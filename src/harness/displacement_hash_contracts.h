// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "scene/shader_graph.h"
#include "scene/shader_nodes.h"
#include <iostream>
#include <stdexcept>

namespace cycles_houdini::harness {
inline void displacement_hash_contracts() {
  int checks = 0;
  const auto require = [&](bool value, const char *message) {
    ++checks;
    if (!value)
      throw std::runtime_error(message);
  };
  const auto graph = [](bool surface_first, bool normal_branch, bool reverse) {
    auto result = ccl::make_unique<ccl::ShaderGraph>();
    ccl::DiffuseBsdfNode *diffuse = nullptr;
    if (surface_first)
      diffuse = result->create_node<ccl::DiffuseBsdfNode>();
    if (normal_branch) {
      auto *normal = result->create_node<ccl::NormalMapNode>();
      if (diffuse)
        result->connect(normal->output("Normal"), diffuse->input("Normal"));
    }
    ccl::ValueNode *height = nullptr;
    ccl::DisplacementNode *displacement = nullptr;
    if (reverse) {
      displacement = result->create_node<ccl::DisplacementNode>();
      height = result->create_node<ccl::ValueNode>();
    } else {
      height = result->create_node<ccl::ValueNode>();
      displacement = result->create_node<ccl::DisplacementNode>();
    }
    height->set_value(.2f);
    result->connect(height->output("Value"), displacement->input("Height"));
    result->connect(displacement->output("Displacement"), result->output()->input("Displacement"));
    if (!diffuse)
      diffuse = result->create_node<ccl::DiffuseBsdfNode>();
    result->connect(diffuse->output("BSDF"), result->output()->input("Surface"));
    result->compute_displacement_hash();
    return result;
  };
  const auto baseline = graph(true, true, false)->displacement_hash;
  require(!baseline.empty(), "Connected displacement has an empty hash");
  require(graph(true, false, false)->displacement_hash == baseline,
          "An independent normal branch invalidated displacement");
  require(graph(false, false, true)->displacement_hash == baseline,
          "Graph construction order invalidated displacement");
  auto changed = graph(true, true, false);
  for (auto *node : changed->nodes)
    if (node->type == ccl::ValueNode::get_node_type())
      static_cast<ccl::ValueNode *>(node)->set_value(.3f);
  changed->compute_displacement_hash();
  require(changed->displacement_hash != baseline, "A displacement input change escaped invalidation");
  changed = graph(true, true, false);
  for (auto *node : changed->nodes)
    if (node->type == ccl::DiffuseBsdfNode::get_node_type())
      static_cast<ccl::DiffuseBsdfNode *>(node)->set_roughness(.8f);
  changed->compute_displacement_hash();
  require(changed->displacement_hash == baseline, "A surface parameter invalidated displacement");
  auto root = ccl::make_unique<ccl::ShaderGraph>();
  auto *geometry = root->create_node<ccl::GeometryNode>();
  root->connect(geometry->output("Position"), root->output()->input("Displacement"));
  root->compute_displacement_hash();
  const auto position = root->displacement_hash;
  root->disconnect(root->output()->input("Displacement"));
  root->connect(geometry->output("Normal"), root->output()->input("Displacement"));
  root->compute_displacement_hash();
  require(root->displacement_hash != position, "Changing the root output socket escaped invalidation");
  root->disconnect(root->output()->input("Displacement"));
  root->compute_displacement_hash();
  require(root->displacement_hash.empty(), "Removed displacement retained its hash");
  // Links with identical node values still describe different dependencies.
  auto links = ccl::make_unique<ccl::ShaderGraph>();
  auto *first = links->create_node<ccl::GeometryNode>();
  auto *second = links->create_node<ccl::GeometryNode>();
  auto *add = links->create_node<ccl::VectorMathNode>();
  links->connect(first->output("Position"), add->input("Vector1"));
  links->connect(first->output("Position"), add->input("Vector2"));
  links->connect(add->output("Vector"), links->output()->input("Displacement"));
  links->compute_displacement_hash();
  const auto shared = links->displacement_hash;
  links->disconnect(add->input("Vector2"));
  links->connect(second->output("Normal"), add->input("Vector2"));
  links->compute_displacement_hash();
  require(links->displacement_hash != shared, "A displacement dependency rewire escaped invalidation");
  std::cout << "{\"result\":\"passed\",\"displacement_hash_checks\":" << checks << "}\n";
}
} // namespace cycles_houdini::harness
