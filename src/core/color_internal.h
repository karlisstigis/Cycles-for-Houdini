// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include "color_interop.h"
#include <OpenColorIO/OpenColorIO.h>
#include <stdexcept>
namespace cycles_houdini {
// Explicit observation: synchronously validate referenced file revisions.
std::shared_ptr<const ColorConfiguration> current_color_configuration();
// Hot-path observation: use the last accepted policy for an unchanged host
// config. Disk edits are accepted by explicit checks / the registry's 1s poll.
std::shared_ptr<const ColorConfiguration> presentation_color_configuration();
// Same blackbody approximation and working-space conversion as Cycles nodes.
Float3 blackbody_temperature_color(float kelvin);
OCIO_NAMESPACE::ConstConfigRcPtr owned_color_configuration(const ColorConfiguration &value);
// Installed by the Houdini binding, which knows the bundled default filename
// even when the OCIO environment variable is absent. Never changes host OCIO.
void set_color_configuration_path_resolver(std::function<std::string()> resolver);
std::string color_configuration_path(const ColorConfiguration &value);
std::string render_color_identity();
OCIO_NAMESPACE::ConstConfigRcPtr render_color_configuration();
OCIO_NAMESPACE::ConstConfigRcPtr render_authoring_color_configuration();
PortableInputSpace render_portable_input_space(const std::string &id);
// Only a whole image can be presented directly from reduced-resolution storage.
bool can_present_compact_display(const FrameEvent &frame, int width, int height);
// When compact_size is requested, the image keeps logical dimensions while its
// RGBA and composites use the returned storage dimensions.
DisplayImage display_image_with_storage(const FrameEvent &frame, const DisplaySettings &settings,
                                        DisplayTimings *timings, std::pair<int, int> *compact_size);
class PreviousColorConfiguration : public std::runtime_error {
public:
  PreviousColorConfiguration()
      : std::runtime_error("Previous color configuration: waiting for a new frame (missing aces_interchange role)") {}
};
bool can_convert_working_pixels(const ColorConfiguration &source, const ColorConfiguration &target);
// Convert premultiplied RGB(A) between retained source and target working
// spaces. Alpha and non-color planes are untouched by the caller's policy.
void convert_working_pixels(const ColorConfiguration &source, const ColorConfiguration &target, float *pixels,
                            int width, int height, int channels);
} // namespace cycles_houdini
