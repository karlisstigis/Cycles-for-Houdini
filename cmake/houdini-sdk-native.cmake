# SPDX-License-Identifier: Apache-2.0
# OBJ/SOP and the worker use Houdini's ABI-compatible image/VDB/math libraries.
# Previously these were selected as a side effect of FindUSDHoudini. Preserve
# the providers without importing any USD/Hydra targets or enabling WITH_USD.
set(HOUDINI_FOUND ON)
set(HOUDINI_VERSION_MAJOR ${Houdini_VERSION_MAJOR})
set(BOOST_DEFINITIONS "-DHBOOST_ALL_NO_LIB")

function(ch_sdk_library output)
  find_library(found NAMES ${ARGN}
    PATHS "${HOUDINI_ROOT}/custom/houdini/dsolib"
    NO_DEFAULT_PATH NO_CACHE REQUIRED)
  set(${output} "${found}" PARENT_SCOPE)
endfunction()

ch_sdk_library(CH_OSD_CPU libosdCPU_md osdCPU osdCPU_md)
ch_sdk_library(CH_OSD_GPU libosdGPU_md osdGPU osdGPU_md)
ch_sdk_library(CH_OPENVDB openvdb_sesi)
ch_sdk_library(CH_TBB tbb12)
set(OPENSUBDIV_INCLUDE_DIRS "${HOUDINI_ROOT}/toolkit/include")
set(OPENSUBDIV_LIBRARIES ${CH_OSD_CPU} ${CH_OSD_GPU})
set(OPENVDB_INCLUDE_DIRS "${HOUDINI_ROOT}/toolkit/include")
set(OPENVDB_LIBRARIES ${CH_OPENVDB})
set(NANOVDB_INCLUDE_DIRS "${HOUDINI_ROOT}/toolkit/include")
set(TBB_INCLUDE_DIRS "${HOUDINI_ROOT}/toolkit/include")
set(TBB_LIBRARIES ${CH_TBB})

function(ch_sdk_image_target target library)
  ch_sdk_library(location ${library})
  add_library(${target} UNKNOWN IMPORTED GLOBAL)
  set_target_properties(${target} PROPERTIES
    IMPORTED_LOCATION "${location}"
    INTERFACE_INCLUDE_DIRECTORIES "${HOUDINI_ROOT}/toolkit/include")
endfunction()
ch_sdk_image_target(OpenColorIO::OpenColorIO OpenColorIO_sidefx)
ch_sdk_image_target(OpenEXR::OpenEXR OpenEXR_sidefx)
ch_sdk_image_target(OpenEXR::OpenEXRCore OpenEXRCore_sidefx)
ch_sdk_image_target(OpenImageIO::OpenImageIO_Util OpenImageIO_Util_sidefx)
ch_sdk_image_target(OpenImageIO::OpenImageIO OpenImageIO_sidefx)
set_property(TARGET OpenImageIO::OpenImageIO PROPERTY
  INTERFACE_LINK_LIBRARIES OpenImageIO::OpenImageIO_Util)

# These are the upstream library-provider switches (despite their USD prefix).
# They prevent Blender's bundled libraries from replacing the Houdini SDK.
foreach(component OPENSUBDIV OPENVDB NANOVDB TBB OPENCOLORIO OPENEXR OPENIMAGEIO)
  set(USD_OVERRIDE_${component} ON)
endforeach()
message(STATUS "Native OBJ/SOP dependencies: Houdini SDK, without USD/Hydra")
