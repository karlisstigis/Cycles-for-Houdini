// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "compact_frame_fixture.h"
#include "cycles_houdini/snapshot.h"
#include <algorithm>
#include <cstring>
#include <iostream>
#include <limits>

namespace cycles_houdini::harness {
inline void compact_image_contracts() {
  int checks = 0;
  const auto require = [&](bool value, const char *message) {
    if (!value)
      throw std::runtime_error(message);
    ++checks;
  };
  const auto rejects = [&](auto action, const char *message) {
    try {
      action();
    } catch (const std::invalid_argument &) {
      ++checks;
      return;
    }
    throw std::runtime_error(message);
  };
  auto frame = compact_frame_fixture();
  validate_frame_image(frame);
  // Independent pixel positions, including the asymmetric noninteger Y flip.
  const int columns[] = {0, 0, 0, 1, 1, 2, 2}, rows[] = {0, 0, 0, 1, 1};
  const auto &beauty = frame.planes[0];
  const auto expanded = expand_image_plane(frame, beauty);
  for (int y = 0; y < 5; ++y)
    for (int x = 0; x < 7; ++x)
      require(std::memcmp(expanded.data() + (y * 7 + x) * 4, beauty.pixels.data() + (rows[y] * 3 + columns[x]) * 4,
                          16) == 0,
              "Float preview placement differs");
  std::vector<float> partial(7 * 2 * 4);
  copy_image_plane_rows(frame, beauty, 2, 2, partial.data());
  require(std::memcmp(partial.data(), expanded.data() + 2 * 7 * 4, partial.size() * 4) == 0,
          "Native row expansion differs");
  const auto ids = expand_image_plane(frame, frame.planes.back());
  for (int y = 0; y < 5; ++y)
    for (int x = 0; x < 7; ++x)
      require(std::memcmp(ids.data() + (y * 7 + x) * 4,
                          frame.planes.back().pixels.data() + (rows[y] * 3 + columns[x]) * 4, 16) == 0,
              "Cryptomatte IDs were changed");
  std::vector<std::uint8_t> bytes(3 * 2 * 4);
  for (int i = 0; i < 6; ++i)
    for (int c = 0; c < 4; ++c)
      bytes[i * 4 + c] = std::uint8_t(10 * i + c);
  const auto enlarged = expand_display_pixels(bytes, 3, 2, 7, 5);
  const int top_rows[] = {0, 0, 1, 1, 1};
  for (int y = 0; y < 5; ++y)
    for (int x = 0; x < 7; ++x)
      require(std::memcmp(enlarged.data() + (y * 7 + x) * 4, bytes.data() + (top_rows[y] * 3 + columns[x]) * 4, 4) == 0,
              "Byte preview placement/flip differs");

  auto reference = frame;
  for (auto &plane : reference.planes) {
    plane.pixels = expand_image_plane(frame, plane);
    plane.width = plane.height = 0;
  }
  for (const auto &plane : frame.planes)
    for (const auto &channels : {"rgba", "rgb"}) {
      DisplaySettings options;
      options.pass = plane.pass;
      options.channels = channels;
      options.exposure = .75f;
      options.gamma = 1.4f;
      const auto compact = display_image(frame, options), full = display_image(reference, options);
      require(compact.rgba == full.rgba, "Compact color/data display differs from full float reference");
      require(compact.width == 7 && compact.height == 5 && compact.x == 2 && compact.y == 3,
              "Display lost coverage/crop");
    }
  DisplaySettings alpha;
  alpha.channels = "alpha";
  require(display_image(frame, alpha).rgba == display_image(reference, alpha).rgba, "Compact alpha differs");
  WorkerReply reply;
  reply.frame = std::make_shared<FrameEvent>(frame);
  const auto packet = encode_worker_reply(reply);
  const auto decoded = decode_worker_reply(packet, frame.color_configuration);
  require(decoded.frame->planes.size() == frame.planes.size(), "Compact wire lost passes");
  for (std::size_t i = 0; i < frame.planes.size(); ++i) {
    const auto &a = frame.planes[i];
    const auto &b = decoded.frame->planes[i];
    require(a.width == b.width && a.height == b.height && a.channels == b.channels, "Compact wire lost dimensions");
    require(a.pixels.size() == b.pixels.size() &&
                std::memcmp(a.pixels.data(), b.pixels.data(), a.pixels.size() * 4) == 0,
            "Compact wire changed float IDs/pixels");
  }
  auto old = packet;
  old[4] = std::byte{32};
  rejects([&] { decode_worker_reply(old, frame.color_configuration); }, "Old worker protocol was accepted");
  auto malformed = packet;
  const std::string name = "combined";
  const auto begin = std::search(malformed.begin(), malformed.end(), reinterpret_cast<const std::byte *>(name.data()),
                                 reinterpret_cast<const std::byte *>(name.data() + name.size()));
  require(begin != malformed.end(), "Wire test cannot locate fixture plane");
  const auto offset = std::size_t(begin - malformed.begin()) + name.size() + 4 + 8 + beauty.pixels.size() * 4;
  const int wrong_width = 4;
  std::memcpy(malformed.data() + offset, &wrong_width, 4);
  rejects([&] { decode_worker_reply(malformed, frame.color_configuration); },
          "Malformed compact wire dimensions were accepted");
  auto bad = frame;
  bad.planes[0].width = 0;
  rejects([&] { validate_frame_image(bad); }, "Half-specified compact dimensions accepted");
  bad = frame;
  bad.planes[0].pixels.pop_back();
  rejects([&] { validate_frame_image(bad); }, "Short compact plane accepted");
  bad = frame;
  bad.complete = true;
  rejects([&] { encode_worker_reply(WorkerReply{.frame = std::make_shared<FrameEvent>(bad)}); },
          "Compact final output accepted");
  bad = frame;
  bad.planes.push_back(bad.planes[0]);
  rejects([&] { validate_frame_image(bad); }, "Duplicate image pass accepted");
  rejects([&] { expand_display_pixels(bytes, 3, 2, 2, 5); }, "Invalid display shrink accepted");
  // Pixel sampling must preserve crop/origin and nearest-cell placement
  // without expanding a large logical image just to read four floats.
  auto focus = frame;
  focus.planes = {{"position", 3, {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}, 2, 2}, {"depth", 1, {3, 6, 9, 12}, 2, 2}};
  const int focus_columns[] = {0, 0, 0, 0, 1, 1, 1};
  for (int y = 0; y < 5; ++y)
    for (int x = 0; x < 7; ++x) {
      const auto value = sample_focus(focus, x + 2, y + 3);
      const auto i = rows[y] * 2 + focus_columns[x];
      require(value.position.x == 1 + i * 3 && value.position.y == 2 + i * 3 && value.position.z == 3 + i * 3 &&
                  value.depth == 3 + i * 3,
              "Compact focus sampled a different logical pixel");
    }
  rejects([&] { sample_focus(focus, 1, 3); }, "Focus sampled outside the crop");
  rejects([&] { sample_focus(focus, 2, 8); }, "Focus sampled past the crop");
  auto invalid = focus;
  invalid.planes[1].pixels[0] = std::numeric_limits<float>::quiet_NaN();
  rejects([&] { sample_focus(invalid, 2, 3); }, "Non-finite focus depth accepted");
  invalid = focus;
  invalid.planes[0].pixels[0] = std::numeric_limits<float>::infinity();
  rejects([&] { sample_focus(invalid, 2, 3); }, "Non-finite focus position accepted");
  invalid = focus;
  invalid.planes[1].pixels[0] = 1e10f;
  rejects([&] { sample_focus(invalid, 2, 3); }, "Focus picked the background");
  invalid = focus;
  invalid.planes[1].pixels[0] = 0;
  rejects([&] { sample_focus(invalid, 2, 3); }, "Zero focus depth accepted");
  invalid = focus;
  invalid.planes[1].channels = 2;
  rejects([&] { sample_focus(invalid, 2, 3); }, "Invalid focus channels accepted");
  invalid = focus;
  invalid.planes[1].height = 1;
  invalid.planes[1].pixels.resize(2);
  rejects([&] { sample_focus(invalid, 2, 3); }, "Mismatched focus grids accepted");
  invalid = focus;
  invalid.width = invalid.full_width = 4096;
  invalid.height = invalid.full_height = 2160;
  invalid.x = invalid.y = 0;
  require(sample_focus(invalid, 4095, 2159).position.z == 12 && invalid.planes[0].pixels.size() == 12,
          "Focus expanded a 4K logical image");
  const auto preference = encode_worker_preview({"normal", true});
  const auto request = decode_worker_preview(preference);
  require(request.pass == "normal" && request.focus, "Typed preview lost focus interests");
  auto malformed_preference = preference;
  malformed_preference.back() = std::byte{2};
  rejects([&] { decode_worker_preview(malformed_preference); }, "Invalid preview boolean accepted");
  malformed_preference = preference;
  malformed_preference.pop_back();
  rejects([&] { decode_worker_preview(malformed_preference); }, "Truncated preview accepted");
  WorkerReply focus_reply;
  focus_reply.focus_frame = std::make_shared<FrameEvent>(focus);
  auto focus_decoded = decode_worker_reply(encode_worker_reply(focus_reply), frame.color_configuration);
  require(sample_focus(*focus_decoded.focus_frame, 8, 7).position.z == 12, "Worker lost independent focus storage");
  focus_reply.focus_is_frame = true;
  rejects([&] { decode_worker_reply(encode_worker_reply(focus_reply), frame.color_configuration); },
          "Invalid focus alias accepted");
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks
            << ",\"passes\":5,\"worker_protocol\":" << worker_protocol_version
            << ",\"scope\":\"Independent odd-size/cropped placement, per-pass dimensions, exact ID bits, "
               "color/alpha/data, transport and strict malformed/final rejection\"}\n";
}
} // namespace cycles_houdini::harness
