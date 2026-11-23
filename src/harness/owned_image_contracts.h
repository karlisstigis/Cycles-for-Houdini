// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "shader_fixture.h"
#include "cycles_houdini/snapshot.h"
#include "../core/texture_memory.h"
#include "util/image_metadata.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <iostream>
#include <limits>
#include <thread>

namespace cycles_houdini::harness {
inline void owned_image_contracts(const std::string &device) {
  const auto require = [](bool value, const char *message) {
    if (!value)
      throw std::runtime_error(message);
  };
  const auto image = [](float red) {
    auto value = std::make_shared<TextureImageData>();
    value->source = "op:/owned/test";
    value->width = 2;
    value->height = 2;
    value->channels = 4;
    value->colorspace = "scene_linear";
    value->pixels = {red, 0, 0, 1, 0, .25f, 0, 1, 0, 0, 1.5f, 1, 2.25f, .5f, .125f, 1};
    value->revision = texture_image_revision(*value);
    return value;
  };
  unsigned conversion_checks = 0;
  {
    // Nonlinear RGB plus a nonidentity alpha operation catches both incorrect
    // association order and accidental RGBA processing in the batch path.
    const auto config = OCIO_NAMESPACE::Config::CreateRaw();
    const auto transform = OCIO_NAMESPACE::ExponentTransform::Create();
    const double exponents[] = {2., 1.5, .75, 2.};
    transform->setValue(exponents);
    const auto processor = config->getProcessor(transform)->getDefaultCPUProcessor();
    for (int mode = 0; mode < 4; ++mode) {
      ccl::ImageMetaData metadata;
      metadata.width = 8197; // Two complete blocks plus an uneven tail.
      metadata.height = 1;
      metadata.type = mode == 3 ? ccl::IMAGE_DATA_TYPE_FLOAT : ccl::IMAGE_DATA_TYPE_FLOAT4;
      metadata.ignore_alpha = mode == 1;
      metadata.is_channel_packed = mode == 2;
      const int channels = mode == 3 ? 1 : 4;
      std::vector<float> values(metadata.width * channels);
      const float coverage[] = {0.f, -.25f, .375f, 1.f, 1e-8f};
      for (std::int64_t i = 0; i < metadata.width; ++i) {
        values[i * channels] = float((i % 31) + 1) / 64.f;
        if (channels == 4) {
          values[i * channels + 1] = -.125f;
          values[i * channels + 2] = .75f;
          values[i * channels + 3] = coverage[i % 5];
        }
      }
      const auto original = values;
      convert_texture_pixels(metadata, values.data(), processor);
      for (std::int64_t i = 0; i < metadata.width; ++i) {
        const auto *input = original.data() + i * channels;
        const float alpha = mode == 0 && input[3] > 0.f ? input[3] : 1.f;
        float rgb[] = {input[0] / alpha, input[channels == 4 ? 1 : 0] / alpha, input[channels == 4 ? 2 : 0] / alpha};
        processor->applyRGB(rgb);
        for (int c = 0; c < (channels == 4 ? 3 : 1); ++c) {
          const float expected = std::isfinite(rgb[c]) ? rgb[c] * alpha : 0.f;
          require(std::abs(values[i * channels + c] - expected) <= 1e-5f * std::max(1.f, std::abs(expected)),
                  "Batched texture conversion differs from independent scalar RGB conversion");
          ++conversion_checks;
        }
        if (channels == 4)
          require(values[i * channels + 3] == input[3], "Batched texture conversion changed alpha");
      }
    }
    WorkerReply reply;
    reply.textures.push_back({"material",
                              "input.exr",
                              "ACES2065-1",
                              "colorInteropID",
                              "policy",
                              "revision",
                              {},
                              8197 * 16,
                              "float32 RGBA OpenEXR, 64x64 tiles, mipmaps"});
    const auto decoded = decode_worker_reply(encode_worker_reply(reply), {});
    require(decoded.textures.at(0).portable_decode_bytes == 8197 * 16 &&
                decoded.textures.at(0).portable_cache_format == reply.textures[0].portable_cache_format,
            "Portable texture cost/format was lost in worker reply");
  }
  std::shared_ptr<const TextureImageData> current = image(.125f);
  {
    auto another_time = *current;
    another_time.evaluation_time = 3.;
    require(texture_image_revision(another_time) == current->revision, "Static image contents depend on capture time");
  }
  std::weak_ptr<const TextureImageData> released = current;
  SessionConfig config;
  config.device_id = device;
  config.width = 48;
  config.height = 32;
  config.samples = 4;
  config.threads = 2;
  config.persistent_updates = true;
  config.passes = {"combined", "depth"};
  config.texture_input_policy.resolve_image = [&](const std::string &source, double at) {
    require(source == "op:/owned/test" && at == 0., "Unexpected image request");
    return current;
  };
  auto scene = shader_reference_fixture();
  auto mesh = std::make_shared<MeshData>(*scene.meshes[0]);
  mesh->corner_uv = {{0, 0}, {1, 0}, {1, 1}, {0, 0}, {1, 1}, {0, 1}};
  scene.meshes[0] = mesh;
  ShaderGraphData graph;
  graph.nodes = {{"image",
                  "cycles/image_texture/1",
                  {{"filename", std::string("op:/owned/test")},
                   {"colorspace", std::string("data")},
                   {"interpolation", std::string("closest")}}},
                 {"surface", "cycles/emission/1", {{"strength", 1.f}}}};
  graph.links = {{{"image", "color"}, {"surface", "color"}}};
  graph.surface = {"surface", "emission"};
  scene.materials[0].kind = MaterialKind::Graph;
  scene.materials[0].graph = graph;
  unsigned delta_checks = 0;
  {
    auto desired = std::make_shared<OwnedTexturePolicy>();
    desired->images[{current->source, 0.}] = current;
    WorkerUpdate first;
    texture_image_difference(nullptr, *desired, first);
    require(first.image_upserts.size() == 1 && first.deleted_images.empty(), "First owned image was not sent");
    ++delta_checks;
    auto decoded = decode_worker_update(encode_worker_update(first));
    auto accepted = apply_texture_image_difference({}, decoded);
    require(accepted->images.size() == 1 && accepted->images.begin()->second->pixels == current->pixels,
            "Image wire round-trip lost pixels");
    ++delta_checks;
    SceneSnapshotState references;
    references.materials.emplace(scene.materials[0].key, scene.materials[0]);
    validate_texture_image_references(references, *accepted);
    ++delta_checks;
    WorkerUpdate camera;
    texture_image_difference(accepted.get(), *desired, camera);
    require(camera.image_upserts.empty() && camera.deleted_images.empty(), "Camera edit resends owned pixels");
    auto retained = apply_texture_image_difference(accepted, camera);
    require(retained->images.begin()->second == accepted->images.begin()->second,
            "Unchanged worker image ownership was copied");
    ++delta_checks;
    auto replacement = std::make_shared<OwnedTexturePolicy>();
    replacement->images[{current->source, 0.}] = image(.875f);
    WorkerUpdate changed;
    texture_image_difference(accepted.get(), *replacement, changed);
    auto candidate = apply_texture_image_difference(accepted, changed);
    require(candidate->images.begin()->second->pixels[0] == .875f &&
                accepted->images.begin()->second->pixels[0] == .125f,
            "Candidate image mutated the accepted ledger");
    ++delta_checks;
    WorkerUpdate erase;
    OwnedTexturePolicy empty;
    texture_image_difference(candidate.get(), empty, erase);
    require(erase.deleted_images.size() == 1 && apply_texture_image_difference(candidate, erase)->images.empty(),
            "Unused image was not retired");
    ++delta_checks;
    const auto rejected = [&](auto action) {
      try {
        action();
      } catch (const std::invalid_argument &) {
        ++delta_checks;
        return;
      }
      throw std::runtime_error("Malformed image delta was accepted");
    };
    rejected([&]() {
      WorkerUpdate bad;
      bad.image_upserts = {nullptr};
      apply_texture_image_difference(accepted, bad);
    });
    rejected([&]() {
      WorkerUpdate bad;
      bad.image_upserts = {current, current};
      apply_texture_image_difference(accepted, bad);
    });
    rejected([&]() {
      WorkerUpdate bad;
      bad.deleted_images = {{current->source, 0.}, {current->source, 0.}};
      apply_texture_image_difference(accepted, bad);
    });
    rejected([&]() {
      WorkerUpdate bad;
      bad.deleted_images = {{"op:/missing/resource", 0.}};
      apply_texture_image_difference(accepted, bad);
    });
    rejected([&]() {
      WorkerUpdate bad;
      bad.deleted_images = {{current->source, 0.}};
      bad.image_upserts = {current};
      apply_texture_image_difference(accepted, bad);
    });
    rejected([&]() {
      WorkerUpdate bad;
      auto corrupt = std::make_shared<TextureImageData>(*current);
      corrupt->pixels[0] = .5f;
      bad.image_upserts = {corrupt};
      apply_texture_image_difference(accepted, bad);
    });
    rejected([&]() {
      WorkerUpdate bad;
      auto corrupt = std::make_shared<TextureImageData>(*current);
      corrupt->pixels.pop_back();
      bad.image_upserts = {corrupt};
      apply_texture_image_difference(accepted, bad);
    });
    rejected([&]() {
      WorkerUpdate bad;
      bad.deleted_images = {{current->source, std::numeric_limits<double>::quiet_NaN()}};
      apply_texture_image_difference(accepted, bad);
    });
    rejected([&]() { validate_texture_image_references(references, empty); });
    rejected([&]() { validate_texture_image_references(SceneSnapshotState{}, *accepted); });
    require(accepted->images.begin()->second->pixels[0] == .125f, "Rejected image delta damaged accepted ownership");
    ++delta_checks;
  }
  unsigned comparisons = 0, rejections = 0;
  {
    RenderSession session(config);
    const auto render = [&](const SceneUpdate &update) {
      const auto generation = session.apply(update);
      const auto limit = std::chrono::steady_clock::now() + std::chrono::seconds(20);
      while (true) {
        const auto status = session.status();
        require(status.state != SessionState::Failed, status.error.c_str());
        const auto frame = session.frame();
        if (frame && frame->generation == generation && frame->complete)
          return frame;
        require(std::chrono::steady_clock::now() < limit, "Owned image render timed out");
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
      }
    };
    const auto pixel = [&](const FrameEvent &frame, int x, int y) {
      const auto plane = std::find_if(frame.planes.begin(), frame.planes.end(),
                                      [](const auto &value) { return value.pass == "combined"; });
      require(plane != frame.planes.end(), "Owned image fixture has no beauty plane");
      const auto &values = plane->pixels;
      const auto at = 4 * (y * 48 + x);
      return Float3{values[at], values[at + 1], values[at + 2]};
    };
    const auto compare = [&](const Float3 &got, const Float3 &expected) {
      require(std::max({std::abs(got.x - expected.x), std::abs(got.y - expected.y), std::abs(got.z - expected.z)}) <
                  1e-5f,
              "Owned image differs from analytic texels");
      ++comparisons;
    };
    auto frame = render(scene);
    compare(pixel(*frame, 12, 8), {.125f, 0, 0});
    compare(pixel(*frame, 36, 8), {0, .25f, 0});
    compare(pixel(*frame, 12, 24), {0, 0, 1.5f});
    compare(pixel(*frame, 36, 24), {2.25f, .5f, .125f});
    const auto conversions = session.status().geometry_conversions;
    current = image(.75f);
    frame = render({});
    compare(pixel(*frame, 12, 8), {.75f, 0, 0});
    require(session.status().geometry_conversions == conversions, "Color image update converted geometry");

    // Source failure is recoverable; a malformed owned layout is not.
    auto failed = std::make_shared<TextureImageData>();
    failed->source = "op:/owned/test";
    failed->error = "Intentional unavailable source";
    failed->revision = texture_image_revision(*failed);
    current = failed;
    frame = render({});
    require(!session.status().source_diagnostics.empty(), "Missing image diagnostic was lost");
    compare(pixel(*frame, 12, 8), {1, 0, 1});
    require(session.status().geometry_conversions == conversions, "Image failure removed/reconverted geometry");
    current = image(.375f);
    frame = render({});
    compare(pixel(*frame, 12, 8), {.375f, 0, 0});
    require(session.status().source_diagnostics.empty(), "Image repair did not clear its diagnostic");
    const auto healthy = current;
    const auto generation = session.status().generation;
    for (int bad = 0; bad < 7; ++bad) {
      auto invalid = std::make_shared<TextureImageData>(*healthy);
      if (bad == 0)
        invalid->width = 0;
      else if (bad == 1)
        invalid->pixels.pop_back();
      else if (bad == 2)
        invalid->pixels[0] = std::numeric_limits<float>::quiet_NaN();
      else if (bad == 3)
        invalid->channels = 5;
      else if (bad == 4)
        invalid->evaluation_time = 1.;
      else if (bad == 5)
        invalid->source = "op:/another/source";
      invalid->revision = texture_image_revision(*invalid);
      current = invalid;
      if (bad == 6)
        invalid->revision = "invalid-revision";
      try {
        session.apply({});
        throw std::runtime_error("Malformed image was accepted");
      } catch (const SceneUpdateRejected &) {
        ++rejections;
      }
      require(session.status().generation == generation, "Rejected image changed the accepted scene");
    }
    current = healthy;
    session.close();
  }
  require(released.expired(), "Engine retained a superseded owned image after shutdown");
  std::cout
      << "{\"result\":\"passed\",\"analytic_texels\":" << comparisons << ",\"safe_rejections\":" << rejections
      << ",\"image_delta_checks\":" << delta_checks << ",\"portable_conversion_checks\":" << conversion_checks
      << ",\"scope\":\"Owned engine image loader only; native COP transport and IPR integration are separate\"}\n";
}
} // namespace cycles_houdini::harness
