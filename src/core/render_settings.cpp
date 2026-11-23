// SPDX-License-Identifier: Apache-2.0
#include "render_settings.h"
#include "session/session.h"
#include "scene/scene.h"
#include "scene/integrator.h"
#include "scene/light.h"
#include "scene/film.h"
#include "util/hash.h"
#include <cmath>
#include <bit>
#include <limits>
#include <stdexcept>
#include <cstdlib>
#include <cstdio>

namespace cycles_houdini {
namespace {
ccl::SamplingPattern pattern(const std::string &token, bool interactive) {
  if (token == "auto")
    return interactive ? ccl::SAMPLING_PATTERN_BLUE_NOISE_FIRST : ccl::SAMPLING_PATTERN_BLUE_NOISE_PURE;
  if (token == "tabulated_sobol")
    return ccl::SAMPLING_PATTERN_TABULATED_SOBOL;
  if (token == "blue_noise")
    return ccl::SAMPLING_PATTERN_BLUE_NOISE_PURE;
  if (token == "blue_noise_first")
    return ccl::SAMPLING_PATTERN_BLUE_NOISE_FIRST;
  if (token == "sobol_burley")
    return ccl::SAMPLING_PATTERN_SOBOL_BURLEY;
  throw std::invalid_argument("Unsupported Cycles sampling pattern: " + token);
}
ccl::FilterType filter(const std::string &token) {
  if (token == "box")
    return ccl::FILTER_BOX;
  if (token == "gaussian")
    return ccl::FILTER_GAUSSIAN;
  if (token == "blackman_harris")
    return ccl::FILTER_BLACKMAN_HARRIS;
  throw std::invalid_argument("Unsupported Cycles pixel filter: " + token);
}
} // namespace
void validate_render_settings(const RenderSettings &s) {
  const auto *experiment = std::getenv("CYCLES_HOUDINI_BDPT_PROTOTYPE");
  if (experiment && std::string(experiment) == "1") {
#ifndef WITH_BDPT_PROTOTYPE
    throw std::invalid_argument("This build does not contain the optional BDPT prototype");
#else
    if (s.light_tree || s.adaptive_sampling || s.fast_gi || s.max_bounce < 2 || s.max_bounce > 8 ||
        s.diffuse_bounce < s.max_bounce || s.glossy_bounce < s.max_bounce || s.transmission_bounce < s.max_bounce ||
        s.filter_glossy != 0 || s.clamp_direct != 0 || s.clamp_indirect != 0 || s.glossy_lobe_floor != 0 ||
        s.resampled_light_sampling || !s.caustics_reflective || !s.caustics_refractive)
      throw std::invalid_argument(
          "The isolated BDPT prototype requires 2..8 total bounces, per-type limits at least that high, both caustics "
          "enabled, and Light Tree, adaptive sampling, Fast GI, glossy filtering and clamping off");
#endif
  }
  if (s.samples < 1 || s.samples > ccl::Integrator::MAX_SAMPLES || s.min_samples < 0 ||
      s.min_samples > ccl::Integrator::MAX_SAMPLES)
    throw std::invalid_argument("Cycles sample counts are outside the supported range");
  if (!std::isfinite(s.time_limit) || s.time_limit < 0)
    throw std::invalid_argument("Render time limit must be finite and nonnegative");
  if (!std::isfinite(s.noise_threshold) || s.noise_threshold < 0 || s.noise_threshold > 1)
    throw std::invalid_argument("Noise threshold must be between zero and one");
  for (int bounce : {s.min_bounce, s.max_bounce, s.diffuse_bounce, s.glossy_bounce, s.transmission_bounce,
                     s.volume_bounce, s.transparent_min_bounce, s.transparent_max_bounce, s.ao_bounces})
    if (bounce < 0 || bounce > 1024)
      throw std::invalid_argument("Light-path bounce limits must be between zero and 1024");
  for (float v : {s.light_threshold, s.clamp_direct, s.clamp_indirect, s.filter_glossy, s.ao_factor, s.film_exposure})
    if (!std::isfinite(v) || v < 0)
      throw std::invalid_argument("Lighting/filter/exposure values must be finite and nonnegative");
  if (!std::isfinite(s.glossy_lobe_floor) || s.glossy_lobe_floor < 0 || s.glossy_lobe_floor > 0.95f)
    throw std::invalid_argument("Glossy Lobe Floor must be between 0 and 0.95");
#ifndef CYCLES_HOUDINI_HAS_PT_RIS_NEE
  if (s.resampled_light_sampling)
    throw std::invalid_argument("Resampled Light Sampling is not part of this build; turn it off");
#endif
#ifndef WITH_BDPT_PROTOTYPE
  if (s.glossy_lobe_floor != 0)
    throw std::invalid_argument("Glossy Lobe Floor is only available in the experimental build; set it to 0");
#else
  if (s.glossy_lobe_floor != 0 && s.clamp_direct != 0)
    throw std::invalid_argument("Glossy Lobe Floor cannot be combined with Clamp Direct yet; set Clamp Direct to 0 "
                                "(Clamp Indirect is supported)");
#endif
  for (float v : {s.ao_distance, s.volume_step_rate, s.filter_width})
    if (!std::isfinite(v) || v <= 0)
      throw std::invalid_argument("AO distance, volume step rate and filter width must be finite and positive");
  if (s.volume_max_steps < 1 || s.volume_max_steps > 1048576)
    throw std::invalid_argument("Volume max steps must be between one and 1048576");
  if (!std::isfinite(s.mist_start) || s.mist_start < 0 || !std::isfinite(s.mist_depth) || s.mist_depth <= 0 ||
      (s.mist_falloff != 0.5f && s.mist_falloff != 1 && s.mist_falloff != 2))
    throw std::invalid_argument(
        "Mist requires nonnegative start, positive depth and quadratic/linear/inverse-quadratic falloff");
  if (!std::isfinite(s.pass_alpha_threshold) || s.pass_alpha_threshold < 0 || s.pass_alpha_threshold > 1)
    throw std::invalid_argument("Pass alpha threshold must be between zero and one");
  if (s.seed < 0 || !std::isfinite(s.frame) || s.frame < std::numeric_limits<int>::min() + 1.0 ||
      s.frame > std::numeric_limits<int>::max() - 1.0)
    throw std::invalid_argument("Invalid sampling seed/frame");
  if (s.fast_gi_method != "replace" && s.fast_gi_method != "add")
    throw std::invalid_argument("Fast GI method must be replace or add");
  if (s.subset_offset < 0 || s.subset_length < 1 ||
      std::int64_t(s.subset_offset) + s.subset_length > ccl::Integrator::MAX_SAMPLES ||
      (s.sample_subset && s.subset_offset >= s.samples))
    throw std::invalid_argument("Sample subset must overlap the total sample budget");
  pattern(s.sampling_pattern, false);
  filter(s.pixel_filter);
}
void configure_render_settings(const RenderSettings &s, ccl::Session &session, ccl::SessionParams &params,
                               bool interactive) {
  auto *scene = session.scene.get();
  auto *integrator = scene->integrator;
  const bool cpu_guiding = s.use_guiding && params.device.type == ccl::DEVICE_CPU;
  if (cpu_guiding && !params.device.has_guiding)
    throw std::invalid_argument("Path Guiding requires OpenPGL support on the selected CPU device");
  integrator->set_use_guiding(cpu_guiding);
  integrator->set_deterministic_guiding(s.deterministic_guiding);
#ifdef WITH_BDPT_PROTOTYPE
  const auto *experiment = std::getenv("CYCLES_HOUDINI_BDPT_PROTOTYPE");
  integrator->set_use_bidirectional_path_tracing(experiment && std::string(experiment) == "1");
#endif
  const bool light_tree_changed = s.light_tree != integrator->get_use_light_tree();
  integrator->set_aa_samples(s.samples);
  integrator->set_use_adaptive_sampling(s.adaptive_sampling);
  integrator->set_adaptive_threshold(s.noise_threshold);
  integrator->set_adaptive_min_samples(s.min_samples);
  integrator->set_sampling_pattern(pattern(s.sampling_pattern, interactive));
  auto seed = std::uint32_t(s.seed);
  if (s.animate_seed) {
    const double integer = std::floor(s.frame), fraction = s.frame - integer;
    seed = ccl::hash_uint2(std::uint32_t(int(integer)), std::uint32_t(s.seed));
    if (fraction != 0)
      seed += ccl::hash_uint2(std::uint32_t(fraction * std::numeric_limits<int>::max()), std::uint32_t(s.seed));
  }
  integrator->set_seed(std::bit_cast<int>(seed));
  integrator->set_min_bounce(s.min_bounce);
  integrator->set_max_bounce(s.max_bounce);
  integrator->set_max_diffuse_bounce(s.diffuse_bounce);
  integrator->set_max_glossy_bounce(s.glossy_bounce);
  integrator->set_max_transmission_bounce(s.transmission_bounce);
  integrator->set_max_volume_bounce(s.volume_bounce);
  integrator->set_transparent_min_bounce(s.transparent_min_bounce);
  integrator->set_transparent_max_bounce(s.transparent_max_bounce);
  integrator->set_use_light_tree(s.light_tree);
  integrator->set_light_sampling_threshold(s.light_threshold);
  if (light_tree_changed)
    scene->light_manager->tag_update(scene, ccl::LightManager::UPDATE_ALL);
  integrator->set_sample_clamp_direct(s.clamp_direct);
  integrator->set_sample_clamp_indirect(s.clamp_indirect);
  integrator->set_filter_glossy(s.filter_glossy);
#ifdef CYCLES_HOUDINI_HAS_PT_RIS_NEE
  integrator->set_pt_ris_nee(s.resampled_light_sampling ? 1 : 0); // Resampled Light Sampling (engine patch 0073)
#endif
  integrator->set_nested_dielectrics(s.nested_dielectrics); // Nested dielectrics (engine patch 0074)
#ifdef WITH_BDPT_PROTOTYPE
  {
    // Experimental lobe floor: the isolated GPU path-trace host reads this at each render_samples call.
    // A changed value is a changed RenderSettings, which restarts the render.
    char text[32];
    std::snprintf(text, sizeof(text), "%.9g", double(s.glossy_lobe_floor));
    const char *value = s.glossy_lobe_floor > 0 ? text : "0";
#ifdef _WIN32
    _putenv_s("CYCLES_HOUDINI_PT_LOBE_FLOOR", value);
#else
    setenv("CYCLES_HOUDINI_PT_LOBE_FLOOR", value, 1);
#endif
  }
#endif
  integrator->set_caustics_reflective(s.caustics_reflective);
  integrator->set_caustics_refractive(s.caustics_refractive);
  const bool replace = s.fast_gi && s.fast_gi_method == "replace", add = s.fast_gi && s.fast_gi_method == "add";
  integrator->set_ao_bounces(replace ? s.ao_bounces : 0);
  integrator->set_ao_factor(replace ? s.ao_factor : 0);
  integrator->set_ao_additive_factor(add ? s.ao_factor : 0);
  integrator->set_ao_distance(s.ao_distance);
  integrator->set_volume_step_rate(s.volume_step_rate);
  integrator->set_volume_max_steps(s.volume_max_steps);
  integrator->set_use_sample_subset(s.sample_subset);
  integrator->set_sample_subset_offset(s.subset_offset);
  integrator->set_sample_subset_length(s.subset_length);
  // Keep update dependencies selective, as Blender's synchronizer does.
  integrator->tag_update(scene, ccl::Integrator::UPDATE_NONE);
  scene->film->set_exposure(s.film_exposure);
  scene->film->set_filter_type(filter(s.pixel_filter));
  scene->film->set_filter_width(s.filter_width);
  scene->film->set_pass_alpha_threshold(s.pass_alpha_threshold);
  scene->film->set_mist_start(s.mist_start);
  scene->film->set_mist_depth(s.mist_depth);
  scene->film->set_mist_falloff(s.mist_falloff);
  params.samples = s.samples;
  params.time_limit = s.time_limit;
  params.use_sample_subset = s.sample_subset;
  params.sample_subset_offset = s.subset_offset;
  params.sample_subset_length = s.subset_length;
}
} // namespace cycles_houdini
