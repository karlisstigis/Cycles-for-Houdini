# SPDX-License-Identifier: Apache-2.0
# The standalone dependency option still uses WITH_CYCLES_EMBREE in the 5.3 RC,
# while the engine compile/link gates now use Blender's WITH_EMBREE spelling.
# Keep one user-facing option and mirror it without creating a second cache knob.
option(WITH_CYCLES_EMBREE "Build with Embree support" ON)
set(WITH_EMBREE "${WITH_CYCLES_EMBREE}")

function(ch_validate_cycles_embree engine_root)
  if(WITH_CYCLES_EMBREE)
    get_directory_property(_ch_kernel_definitions DIRECTORY "${engine_root}/src" COMPILE_DEFINITIONS)
    if(NOT "WITH_EMBREE" IN_LIST _ch_kernel_definitions)
      message(FATAL_ERROR "CPU Embree was requested but the Cycles kernel was configured without WITH_EMBREE")
    endif()
    set(_ch_embree_version_definitions "${_ch_kernel_definitions}")
    list(FILTER _ch_embree_version_definitions INCLUDE REGEX "^EMBREE_MAJOR_VERSION=[0-9]+$")
    if(NOT _ch_embree_version_definitions)
      message(FATAL_ERROR "CPU Embree was requested but the Cycles kernel has no Embree API version")
    endif()
  endif()
  message(STATUS "Cycles CPU Embree: ${WITH_CYCLES_EMBREE}")
endfunction()
