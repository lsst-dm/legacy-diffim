/*
 * LSST Data Management System
 *
 * This product includes software developed by the
 * LSST Project (http://www.lsst.org/).
 * See the COPYRIGHT file
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the LSST License Statement and
 * the GNU General Public License along with this program.  If not,
 * see <https://www.lsstcorp.org/LegalNotices/>.
 */
#include "pybind11/pybind11.h"
#include "lsst/cpputils/python.h"
#include "pybind11/eigen.h"

#include "ndarray/pybind11.h"

#include "lsst/afw/image/Image.h"
#include "lsst/afw/image/MaskedImage.h"
#include "lsst/afw/math/Function.h"
#include "lsst/afw/math/Kernel.h"
#include "lsst/ip/diffim/ImageSubtract.h"

namespace py = pybind11;
using namespace pybind11::literals;

namespace lsst {
namespace ip {
namespace diffim {

namespace {

/**
 * Wrap convolveAndSubtract function for a pixel type and background type
 *
 * @tparam PixelT  pixel type for Image and image plane of MaskedImage
 * @tparam BackgroundT  type of background; instantiate for both:
 *                      - `double` for a constant background
 *                      - `afw::math::Function2<double> const &` for a spatially varying background
 * @param mod  pybind11 module
 */
template <typename PixelT, typename BackgroundT>
void declareConvolveAndSubtract(lsst::cpputils::python::WrapperCollection &wrappers) {
    auto &mod = wrappers.module;
    mod.def("convolveAndSubtract",
            (afw::image::MaskedImage<PixelT>(*)(afw::image::MaskedImage<PixelT> const &,
                                                afw::image::MaskedImage<PixelT> const &,
                                                afw::math::Kernel const &, BackgroundT, bool)) &
                    convolveAndSubtract,
            "templateImage"_a, "scienceMaskedImage"_a, "convolutionKernel"_a, "background"_a,
            "invert"_a = true);

    mod.def("convolveAndSubtract",
            (afw::image::MaskedImage<PixelT>(*)(afw::image::Image<PixelT> const &,
                                                afw::image::MaskedImage<PixelT> const &,
                                                afw::math::Kernel const &, BackgroundT, bool)) &
                    convolveAndSubtract,
            "templateImage"_a, "scienceMaskedImage"_a, "convolutionKernel"_a, "background"_a,
            "invert"_a = true);
}

}  // namespace lsst::ip::diffim::<anonymous>

void wrapImageSubtract(lsst::cpputils::python::WrapperCollection &wrappers) {
    declareConvolveAndSubtract<float, double>(wrappers);
    declareConvolveAndSubtract<float, afw::math::Function2<double> const &>(wrappers);
}

}  // diffim
}  // ip
}  // lsst
