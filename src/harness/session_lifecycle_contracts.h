// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <chrono>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <thread>

namespace cycles_houdini::harness {
inline void session_lifecycle_contracts(const std::string &device) {
  int checks = 0;
  const auto require = [&](bool condition, const char *message) {
    ++checks;
    if (!condition)
      throw std::runtime_error(message);
  };
  const auto complete = [&](RenderSession &session, std::uint64_t generation) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(90);
    while (std::chrono::steady_clock::now() < deadline) {
      const auto status = session.status();
      require(status.state != SessionState::Failed, "Lifecycle render failed");
      const auto image = session.frame();
      if (image && image->generation == generation && image->complete)
        return image;
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    throw std::runtime_error("Lifecycle render timed out");
  };
  const auto equal_pixels = [&](const FrameEvent &actual, const FrameEvent &expected) {
    require(actual.width == expected.width && actual.height == expected.height, "Lifecycle raster changed");
    require(actual.planes.size() == expected.planes.size(), "Lifecycle pass count changed");
    for (std::size_t i = 0; i < actual.planes.size(); ++i) {
      const auto &a = actual.planes[i];
      const auto &b = expected.planes[i];
      require(a.pass == b.pass && a.channels == b.channels && a.pixels.size() == b.pixels.size(),
              "Lifecycle pass layout changed");
      require(std::memcmp(a.pixels.data(), b.pixels.data(), a.pixels.size() * sizeof(float)) == 0,
              "Lifecycle pixels changed");
    }
  };
  for (bool persistent : {false, true}) {
    SessionConfig config;
    config.device_id = device;
    config.width = 24;
    config.height = 16;
    config.samples = 4;
    config.persistent_updates = persistent;
    config.collect_diagnostics = true;
    config.passes = {"combined", "depth", "normal"};
    RenderSession owner(config), peer(config);
    // apply borrows the command only until the synchronous boundary returns.
    // Both engines must retain resources after this temporary is destroyed.
    const auto first = complete(owner, owner.apply(calibration_fixture()));
    const auto peer_first = complete(peer, peer.apply(calibration_fixture()));
    equal_pixels(*first, *peer_first);
    require(first->color_configuration == peer_first->color_configuration &&
                first->color_epoch == peer_first->color_epoch,
            "Two owners disagree on the process color epoch");
    const auto peer_generation = peer.status().generation;
    const auto reject = [&](const SceneUpdate &update) {
      const auto before = owner.status();
      const auto image = owner.frame();
      bool rejected = false;
      try {
        owner.apply(update);
      } catch (const SceneUpdateRejected &) {
        rejected = true;
      }
      require(rejected, "Invalid candidate did not report SceneUpdateRejected");
      const auto after = owner.status();
      require(after.generation == before.generation && after.state == before.state &&
                  after.color_update_pending == before.color_update_pending,
              "Rejected candidate changed owner state");
      require(owner.frame() == image, "Rejected candidate replaced the immutable accepted frame");
      require(peer.status().generation == peer_generation && peer.frame() == peer_first,
              "Rejected candidate changed its peer");
    };
    SceneUpdate invalid_pass;
    invalid_pass.passes = std::vector<std::string>{"combined", "unknown_lifecycle_pass"};
    reject(invalid_pass);
    SceneUpdate invalid_geometry;
    invalid_geometry.deleted_geometry = {"calibration/triangle"};
    reject(invalid_geometry);
    owner.pause(true);
    reject(invalid_geometry);
    require(owner.status().state == SessionState::Paused, "Rejection lost pause");
    owner.pause(false);
    require(owner.apply({}, true) == first->generation, "Identical candidate did not reuse the accepted generation");
    owner.close();
    owner.close();
    require(owner.status().state == SessionState::Closed, "Closing an owner was not idempotent");
    // Detaching one participant must leave the registry and other owner live.
    SceneUpdate moved;
    moved.camera = CameraData{};
    moved.camera->transform[3] = .125f;
    const auto changed = complete(peer, peer.apply(moved));
    require(changed->generation == peer_generation + 1, "Surviving owner did not advance");
    RenderSession reference(config);
    auto scene = calibration_fixture();
    scene.camera = moved.camera;
    const auto expected = complete(reference, reference.apply(scene));
    equal_pixels(*changed, *expected);
    require(first->complete && first->generation == 1, "Retained frame ownership changed after close");
    peer.close();
    reference.close();
  }
  std::cout << "{\"session_lifecycle_contracts\":\"passed\",\"checks\":" << checks << "}\n";
}
} // namespace cycles_houdini::harness
