// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include "../core/color_internal.h"
#include <bit>

namespace cycles_houdini::harness {
inline FrameEvent compact_frame_fixture() {
  FrameEvent frame;
  frame.width = 7;
  frame.height = 5;
  frame.x = 2;
  frame.y = 3;
  frame.full_width = 13;
  frame.full_height = 11;
  frame.rendered_width = 3;
  frame.rendered_height = 2;
  frame.pixel_aspect = 1.6f;
  frame.samples = 2;
  frame.generation = 7;
  frame.sequence = 11;
  frame.color_configuration = current_color_configuration();
  const std::vector<float> rgba = {.1f, .2f, .3f, 1, .2f,  .1f, 0,    .5f,  .4f, 0,   .2f, 0,
                                   0,   .3f, .1f, 1, .15f, .1f, .05f, .25f, .4f, .3f, .2f, 1};
  frame.planes.push_back({"combined", 4, rgba, 3, 2});
  frame.planes.push_back({"noisy_combined", 4, rgba, 2, 3});
  frame.planes.push_back({"depth", 1, {.125f, .25f, .5f, 1}, 2, 2});
  frame.planes.push_back({"normal", 3, {1, 0, 0, 0, 1, 0, 0, 0, 1, .2f, .4f, .6f, .8f, .6f, .4f, 0, 1, 1}, 2, 3});
  std::vector<float> crypto;
  for (const std::uint32_t id : {0x3f812345u, 0x7fc54321u, 0x80000000u, 0x01234567u, 0x76543210u, 0xffc12345u}) {
    crypto.insert(crypto.end(), {std::bit_cast<float>(id), .75f, std::bit_cast<float>(id ^ 0x12345u), .25f});
  }
  frame.planes.push_back({"crypto_object00", 4, std::move(crypto), 3, 2});
  for (const auto &plane : frame.planes) {
    PassDescriptor pass;
    pass.id = plane.pass;
    pass.filtered = true;
    pass.color = plane.pass == "combined" || plane.pass == "noisy_combined";
    pass.cryptomatte = plane.pass == "crypto_object00";
    pass.channels = plane.channels == 1   ? std::vector<std::string>{"Z"}
                    : plane.channels == 3 ? std::vector<std::string>{"R", "G", "B"}
                                          : std::vector<std::string>{"R", "G", "B", "A"};
    frame.requested_passes.push_back(std::move(pass));
  }
  return frame;
}
} // namespace cycles_houdini::harness
