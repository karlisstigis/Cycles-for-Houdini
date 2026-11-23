// SPDX-License-Identifier: Apache-2.0
#include "denoising.h"
#include "util/openimagedenoise.h"
#ifdef WITH_OPENIMAGEDENOISE
#include "integrator/denoiser_oidn_gpu.h"
#endif
#ifdef WITH_OPTIX
#include "integrator/denoiser_optix.h"
#endif
#ifdef WITH_DLSS
#include "integrator/denoiser_dlss.h"
#endif
#include <cmath>
#include <algorithm>
#include <stdexcept>

namespace cycles_houdini {
void enable_renderer_device_probes() {
#ifdef WITH_DLSS
  ccl::DLSSDenoiser::enable_device_probe();
#endif
}

std::vector<std::string> available_denoisers(const ccl::DeviceInfo &device) {
  std::vector<std::string> result;
#ifdef WITH_OPENIMAGEDENOISE
  if (device.type != ccl::DEVICE_CPU && ccl::OIDNDenoiserGPU::is_device_supported(device))
    result.push_back("oidn_gpu");
#endif
#ifdef WITH_OPTIX
  if (ccl::OptiXDenoiser::is_device_supported(device))
    result.push_back("optix");
#endif
#ifdef WITH_DLSS
  const bool dlss = device.type == ccl::DEVICE_MULTI
                        ? std::any_of(device.multi_devices.begin(), device.multi_devices.end(),
                                      [](const auto &child) { return ccl::DLSSDenoiser::is_device_supported(child); })
                        : ccl::DLSSDenoiser::is_device_supported(device);
  if (dlss)
    result.push_back("dlss");
#endif
  if (ccl::openimagedenoise_supported())
    result.push_back("oidn_cpu");
  return result;
}
DenoisePolicy resolve_denoising(const DenoiseSettings &settings, const ccl::DeviceInfo &device) {
  if (settings.method != "auto" && settings.method != "oidn" && settings.method != "optix" && settings.method != "dlss")
    throw std::invalid_argument("Denoiser must be auto, oidn, optix or dlss");
  if (settings.start_sample < 0)
    throw std::invalid_argument("Denoising start sample must be nonnegative");
  if (!std::isfinite(settings.upscale_factor) || settings.upscale_factor < 1.0f || settings.upscale_factor > 3.0f)
    throw std::invalid_argument("Denoising upscale_factor must be finite and between 1 and 3");
  if (settings.method != "dlss" && settings.upscale_factor != 1.0f)
    throw std::invalid_argument("Denoising upscaling requires DLSS");
  DenoisePolicy result;
  auto &params = result.parameters;
  if (settings.quality == "high")
    params.quality = ccl::DENOISER_QUALITY_HIGH;
  else if (settings.quality == "balanced")
    params.quality = ccl::DENOISER_QUALITY_BALANCED;
  else if (settings.quality == "fast")
    params.quality = ccl::DENOISER_QUALITY_FAST;
  else
    throw std::invalid_argument("Denoising quality must be high, balanced or fast");
  if (settings.prefilter == "accurate")
    params.prefilter = ccl::DENOISER_PREFILTER_ACCURATE;
  else if (settings.prefilter == "fast")
    params.prefilter = ccl::DENOISER_PREFILTER_FAST;
  else if (settings.prefilter == "none")
    params.prefilter = ccl::DENOISER_PREFILTER_NONE;
  else
    throw std::invalid_argument("Denoising prefilter must be accurate, fast or none");
  params.use = settings.enabled;
  params.start_sample = settings.start_sample;
  if (settings.passes == "rgb")
    params.passes = ccl::DENOISER_PASS_NONE;
  else if (settings.passes == "albedo")
    params.passes = ccl::DENOISER_PASS_ALBEDO;
  else if (settings.passes == "albedo_normal")
    params.passes = ccl::DENOISER_PASS_ALBEDO | ccl::DENOISER_PASS_NORMAL;
  else
    throw std::invalid_argument("Denoising passes must be rgb, albedo or albedo_normal");
  if (!settings.enabled)
    return result;
  const auto choices = available_denoisers(device);
  const auto supports = [&](const std::string &name) {
    return std::find(choices.begin(), choices.end(), name) != choices.end();
  };
  if (settings.method == "dlss") {
    if (!supports("dlss"))
      throw std::invalid_argument(
          "DLSS Ray Reconstruction is unavailable on the selected render device: this build needs DLSS SDK support and "
          "a compatible NVIDIA RTX GPU/NGX driver; no denoiser fallback is applied");
    result.backend = "dlss";
    params.start_sample = 0;
    params.upscale_factor = settings.upscale_factor;
    params.passes = ccl::DENOISER_PASS_ALBEDO | ccl::DENOISER_PASS_SPECULAR_ALBEDO | ccl::DENOISER_PASS_NORMAL |
                    ccl::DENOISER_PASS_ROUGHNESS | ccl::DENOISER_PASS_DEPTH | ccl::DENOISER_PASS_BACKWARD_MOTION |
                    ccl::DENOISER_PASS_SPECULAR_MOTION;
  } else if (settings.method == "optix") {
    if (!supports("optix"))
      throw std::invalid_argument("OptiX denoising requires an available OptiX render GPU; no CPU fallback is applied");
    result.backend = "optix";
  } else if (device.type == ccl::DEVICE_CPU || !settings.use_gpu) {
    if (!supports("oidn_cpu"))
      throw std::invalid_argument("OpenImageDenoise CPU is unavailable in this build/on this CPU");
    result.backend = "oidn_cpu";
  } else if (supports("oidn_gpu"))
    result.backend = "oidn_gpu";
  else if (settings.method == "auto" && supports("optix"))
    result.backend = "optix";
  else
    throw std::invalid_argument("Requested GPU denoiser is unavailable on " + device.id +
                                "; explicitly disable Use GPU for CPU OpenImageDenoise");
  params.type = result.backend == "dlss"
                    ? ccl::DENOISER_DLSS
                    : (result.backend == "optix" ? ccl::DENOISER_OPTIX : ccl::DENOISER_OPENIMAGEDENOISE);
  params.use_gpu = result.backend != "oidn_cpu";
  result.device_id = params.use_gpu ? device.id : "CPU";
  return result;
}
} // namespace cycles_houdini
