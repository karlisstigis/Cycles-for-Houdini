// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "shader_graph.h"
namespace ccl {
class Scene;
class ImageMetaData;
} // namespace ccl
namespace cycles_houdini {
// Working float pixels have already been conformed to scalar/RGBA storage.
void convert_texture_pixels(const ccl::ImageMetaData &metadata, void *pixels,
                            const OCIO_NAMESPACE::ConstCPUProcessorRcPtr &processor);
// Called only inside the accepted scene transaction. Preflight owns and checks
// pixels; the engine loader never calls a Houdini or Python provider.
void install_memory_images(ccl::Scene &scene, ccl::ShaderGraph &graph, const ShaderGraphRequirements &requirements);
} // namespace cycles_houdini
