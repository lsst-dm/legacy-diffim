# This file is part of ip_diffim.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import numpy as np

import lsst.daf.base as dafBase
import lsst.pex.config as pexConfig
import lsst.afw.detection as afwDetect
import lsst.afw.image as afwImage
import lsst.afw.math as afwMath
import lsst.afw.geom as afwGeom
import lsst.afw.table as afwTable
import lsst.geom as geom
import lsst.pipe.base as pipeBase
from lsst.meas.algorithms import SourceDetectionTask, SubtractBackgroundTask, WarpedPsf
from lsst.meas.base import SingleFrameMeasurementTask
from .makeKernelBasisList import makeKernelBasisList
from .psfMatch import PsfMatchTask, PsfMatchConfigDF, PsfMatchConfigAL
from . import utils as diffimUtils
from . import diffimLib
from . import diffimTools
import lsst.afw.display as afwDisplay
from lsst.utils.timer import timeMethod

__all__ = ["ImagePsfMatchConfig", "ImagePsfMatchTask", "subtractAlgorithmRegistry"]

sigma2fwhm = 2.*np.sqrt(2.*np.log(2.))


class ImagePsfMatchConfig(pexConfig.Config):
    """Configuration for image-to-image Psf matching.
    """
    kernel = pexConfig.ConfigChoiceField(
        doc="kernel type",
        typemap=dict(
            AL=PsfMatchConfigAL,
            DF=PsfMatchConfigDF
        ),
        default="AL",
    )
    selectDetection = pexConfig.ConfigurableField(
        target=SourceDetectionTask,
        doc="Initial detections used to feed stars to kernel fitting",
    )
    selectMeasurement = pexConfig.ConfigurableField(
        target=SingleFrameMeasurementTask,
        doc="Initial measurements used to feed stars to kernel fitting",
    )

    def setDefaults(self):
        # High sigma detections only
        self.selectDetection.reEstimateBackground = False
        self.selectDetection.thresholdValue = 10.0

        # Minimal set of measurments for star selection
        self.selectMeasurement.algorithms.names.clear()
        self.selectMeasurement.algorithms.names = ('base_SdssCentroid', 'base_PsfFlux', 'base_PixelFlags',
                                                   'base_SdssShape', 'base_GaussianFlux', 'base_SkyCoord')
        self.selectMeasurement.slots.modelFlux = None
        self.selectMeasurement.slots.apFlux = None
        self.selectMeasurement.slots.calibFlux = None


class ImagePsfMatchTask(PsfMatchTask):
    """Psf-match two MaskedImages or Exposures using the sources in the images.

    Parameters
    ----------
    args :
        Arguments to be passed to lsst.ip.diffim.PsfMatchTask.__init__
    kwargs :
        Keyword arguments to be passed to lsst.ip.diffim.PsfMatchTask.__init__

    """

    ConfigClass = ImagePsfMatchConfig

    def __init__(self, *args, **kwargs):
        """Create the ImagePsfMatchTask.
        """
        PsfMatchTask.__init__(self, *args, **kwargs)
        self.kConfig = self.config.kernel.active
        self._warper = afwMath.Warper.fromConfig(self.kConfig.warpingConfig)
        # the background subtraction task uses a config from an unusual location,
        # so cannot easily be constructed with makeSubtask
        self.background = SubtractBackgroundTask(config=self.kConfig.afwBackgroundConfig, name="background",
                                                 parentTask=self)
        self.selectSchema = afwTable.SourceTable.makeMinimalSchema()
        self.selectAlgMetadata = dafBase.PropertyList()
        self.makeSubtask("selectDetection", schema=self.selectSchema)
        self.makeSubtask("selectMeasurement", schema=self.selectSchema, algMetadata=self.selectAlgMetadata)

    @timeMethod
    def matchExposures(self, templateExposure, scienceExposure,
                       templateFwhmPix=None, scienceFwhmPix=None,
                       candidateList=None, doWarping=True, convolveTemplate=True):
        """Warp and PSF-match an exposure to the reference.

        Do the following, in order:

        - Warp templateExposure to match scienceExposure,
            if doWarping True and their WCSs do not already match
        - Determine a PSF matching kernel and differential background model
            that matches templateExposure to scienceExposure
        - Convolve templateExposure by PSF matching kernel

        Parameters
        ----------
        templateExposure : `lsst.afw.image.Exposure`
            Exposure to warp and PSF-match to the reference masked image
        scienceExposure : `lsst.afw.image.Exposure`
            Exposure whose WCS and PSF are to be matched to
        templateFwhmPix :`float`
            FWHM (in pixels) of the Psf in the template image (image to convolve)
        scienceFwhmPix : `float`
            FWHM (in pixels) of the Psf in the science image
        candidateList : `list`, optional
            a list of footprints/maskedImages for kernel candidates;
            if `None` then source detection is run.

            - Currently supported: list of Footprints or measAlg.PsfCandidateF

        doWarping : `bool`
            what to do if ``templateExposure`` and ``scienceExposure`` WCSs do not match:

            - if `True` then warp ``templateExposure`` to match ``scienceExposure``
            - if `False` then raise an Exception

        convolveTemplate : `bool`
            Whether to convolve the template image or the science image:

            - if `True`, ``templateExposure`` is warped if doWarping,
              ``templateExposure`` is convolved
            - if `False`, ``templateExposure`` is warped if doWarping,
              ``scienceExposure`` is convolved

        Returns
        -------
        results : `lsst.pipe.base.Struct`
            An `lsst.pipe.base.Struct` containing these fields:

            - ``matchedImage`` : the PSF-matched exposure =
                Warped ``templateExposure`` convolved by psfMatchingKernel. This has:

                - the same parent bbox, Wcs and PhotoCalib as scienceExposure
                - the same filter as templateExposure
                - no Psf (because the PSF-matching process does not compute one)

            - ``psfMatchingKernel`` : the PSF matching kernel
            - ``backgroundModel`` : differential background model
            - ``kernelCellSet`` : SpatialCellSet used to solve for the PSF matching kernel

        Raises
        ------
        RuntimeError
           Raised if doWarping is False and ``templateExposure`` and
           ``scienceExposure`` WCSs do not match
        """
        if not self._validateWcs(templateExposure, scienceExposure):
            if doWarping:
                self.log.info("Astrometrically registering template to science image")
                templatePsf = templateExposure.getPsf()
                # Warp PSF before overwriting exposure
                xyTransform = afwGeom.makeWcsPairTransform(templateExposure.getWcs(),
                                                           scienceExposure.getWcs())
                psfWarped = WarpedPsf(templatePsf, xyTransform)
                templateExposure = self._warper.warpExposure(scienceExposure.getWcs(),
                                                             templateExposure,
                                                             destBBox=scienceExposure.getBBox())
                templateExposure.setPsf(psfWarped)
            else:
                self.log.error("ERROR: Input images not registered")
                raise RuntimeError("Input images not registered")

        if templateFwhmPix is None:
            if not templateExposure.hasPsf():
                self.log.warning("No estimate of Psf FWHM for template image")
            else:
                templateFwhmPix = diffimUtils.getPsfFwhm(templateExposure.psf)
                self.log.info("templateFwhmPix: %s", templateFwhmPix)

        if scienceFwhmPix is None:
            if not scienceExposure.hasPsf():
                self.log.warning("No estimate of Psf FWHM for science image")
            else:
                scienceFwhmPix = diffimUtils.getPsfFwhm(scienceExposure.psf)
                self.log.info("scienceFwhmPix: %s", scienceFwhmPix)

        if convolveTemplate:
            kernelSize = self.makeKernelBasisList(templateFwhmPix, scienceFwhmPix)[0].getWidth()
            candidateList = self.makeCandidateList(
                templateExposure, scienceExposure, kernelSize, candidateList)
            results = self.matchMaskedImages(
                templateExposure.getMaskedImage(), scienceExposure.getMaskedImage(), candidateList,
                templateFwhmPix=templateFwhmPix, scienceFwhmPix=scienceFwhmPix)
        else:
            kernelSize = self.makeKernelBasisList(scienceFwhmPix, templateFwhmPix)[0].getWidth()
            candidateList = self.makeCandidateList(
                templateExposure, scienceExposure, kernelSize, candidateList)
            results = self.matchMaskedImages(
                scienceExposure.getMaskedImage(), templateExposure.getMaskedImage(), candidateList,
                templateFwhmPix=scienceFwhmPix, scienceFwhmPix=templateFwhmPix)

        psfMatchedExposure = afwImage.makeExposure(results.matchedImage, scienceExposure.getWcs())
        psfMatchedExposure.setFilter(templateExposure.getFilter())
        psfMatchedExposure.setPhotoCalib(scienceExposure.getPhotoCalib())
        results.warpedExposure = templateExposure
        results.matchedExposure = psfMatchedExposure
        return results

    @timeMethod
    def matchMaskedImages(self, templateMaskedImage, scienceMaskedImage, candidateList,
                          templateFwhmPix=None, scienceFwhmPix=None):
        """PSF-match a MaskedImage (templateMaskedImage) to a reference MaskedImage (scienceMaskedImage).

        Do the following, in order:

        - Determine a PSF matching kernel and differential background model
            that matches templateMaskedImage to scienceMaskedImage
        - Convolve templateMaskedImage by the PSF matching kernel

        Parameters
        ----------
        templateMaskedImage : `lsst.afw.image.MaskedImage`
            masked image to PSF-match to the reference masked image;
            must be warped to match the reference masked image
        scienceMaskedImage : `lsst.afw.image.MaskedImage`
            maskedImage whose PSF is to be matched to
        templateFwhmPix : `float`
            FWHM (in pixels) of the Psf in the template image (image to convolve)
        scienceFwhmPix : `float`
            FWHM (in pixels) of the Psf in the science image
        candidateList : `list`, optional
            A list of footprints/maskedImages for kernel candidates;
            if `None` then source detection is run.

            - Currently supported: list of Footprints or measAlg.PsfCandidateF

        Returns
        -------
        result : `callable`
        An `lsst.pipe.base.Struct` containing these fields:

        - psfMatchedMaskedImage: the PSF-matched masked image =
            ``templateMaskedImage`` convolved with psfMatchingKernel.
            This has the same xy0, dimensions and wcs as ``scienceMaskedImage``.
        - psfMatchingKernel: the PSF matching kernel
        - backgroundModel: differential background model
        - kernelCellSet: SpatialCellSet used to solve for the PSF matching kernel

        Raises
        ------
        RuntimeError
            Raised if input images have different dimensions
        """
        import lsstDebug
        display = lsstDebug.Info(__name__).display
        displayTemplate = lsstDebug.Info(__name__).displayTemplate
        displaySciIm = lsstDebug.Info(__name__).displaySciIm
        displaySpatialCells = lsstDebug.Info(__name__).displaySpatialCells
        maskTransparency = lsstDebug.Info(__name__).maskTransparency
        if not maskTransparency:
            maskTransparency = 0
        if display:
            afwDisplay.setDefaultMaskTransparency(maskTransparency)

        if not candidateList:
            raise RuntimeError("Candidate list must be populated by makeCandidateList")

        if not self._validateSize(templateMaskedImage, scienceMaskedImage):
            self.log.error("ERROR: Input images different size")
            raise RuntimeError("Input images different size")

        if display and displayTemplate:
            disp = afwDisplay.Display(frame=lsstDebug.frame)
            disp.mtv(templateMaskedImage, title="Image to convolve")
            lsstDebug.frame += 1

        if display and displaySciIm:
            disp = afwDisplay.Display(frame=lsstDebug.frame)
            disp.mtv(scienceMaskedImage, title="Image to not convolve")
            lsstDebug.frame += 1

        kernelCellSet = self._buildCellSet(templateMaskedImage,
                                           scienceMaskedImage,
                                           candidateList)

        if display and displaySpatialCells:
            diffimUtils.showKernelSpatialCells(scienceMaskedImage, kernelCellSet,
                                               symb="o", ctype=afwDisplay.CYAN, ctypeUnused=afwDisplay.YELLOW,
                                               ctypeBad=afwDisplay.RED, size=4, frame=lsstDebug.frame,
                                               title="Image to not convolve")
            lsstDebug.frame += 1

        if templateFwhmPix and scienceFwhmPix:
            self.log.info("Matching Psf FWHM %.2f -> %.2f pix", templateFwhmPix, scienceFwhmPix)

        if self.kConfig.useBicForKernelBasis:
            tmpKernelCellSet = self._buildCellSet(templateMaskedImage,
                                                  scienceMaskedImage,
                                                  candidateList)
            nbe = diffimTools.NbasisEvaluator(self.kConfig, templateFwhmPix, scienceFwhmPix)
            bicDegrees = nbe(tmpKernelCellSet, self.log)
            basisList = self.makeKernelBasisList(templateFwhmPix, scienceFwhmPix,
                                                 basisDegGauss=bicDegrees[0], metadata=self.metadata)
            del tmpKernelCellSet
        else:
            basisList = self.makeKernelBasisList(templateFwhmPix, scienceFwhmPix,
                                                 metadata=self.metadata)

        spatialSolution, psfMatchingKernel, backgroundModel = self._solve(kernelCellSet, basisList)

        psfMatchedMaskedImage = afwImage.MaskedImageF(templateMaskedImage.getBBox())
        convolutionControl = afwMath.ConvolutionControl()
        convolutionControl.setDoNormalize(False)
        afwMath.convolve(psfMatchedMaskedImage, templateMaskedImage, psfMatchingKernel, convolutionControl)
        return pipeBase.Struct(
            matchedImage=psfMatchedMaskedImage,
            psfMatchingKernel=psfMatchingKernel,
            backgroundModel=backgroundModel,
            kernelCellSet=kernelCellSet,
        )

    @timeMethod
    def subtractExposures(self, templateExposure, scienceExposure,
                          templateFwhmPix=None, scienceFwhmPix=None,
                          candidateList=None, doWarping=True, convolveTemplate=True):
        """Register, Psf-match and subtract two Exposures.

        Do the following, in order:

        - Warp templateExposure to match scienceExposure, if their WCSs do not already match
        - Determine a PSF matching kernel and differential background model
            that matches templateExposure to scienceExposure
        - PSF-match templateExposure to scienceExposure
        - Compute subtracted exposure (see return values for equation).

        Parameters
        ----------
        templateExposure : `lsst.afw.image.ExposureF`
            Exposure to PSF-match to scienceExposure
        scienceExposure : `lsst.afw.image.ExposureF`
            Reference Exposure
        templateFwhmPix : `float`
            FWHM (in pixels) of the Psf in the template image (image to convolve)
        scienceFwhmPix : `float`
            FWHM (in pixels) of the Psf in the science image
        candidateList : `list`, optional
            A list of footprints/maskedImages for kernel candidates;
            if `None` then source detection is run.

            - Currently supported: list of Footprints or measAlg.PsfCandidateF

        doWarping : `bool`
            What to do if ``templateExposure``` and ``scienceExposure`` WCSs do
            not match:

            - if `True` then warp ``templateExposure`` to match ``scienceExposure``
            - if `False` then raise an Exception

        convolveTemplate : `bool`
            Convolve the template image or the science image

            - if `True`, ``templateExposure`` is warped if doWarping,
              ``templateExposure`` is convolved
            - if `False`, ``templateExposure`` is warped if doWarping,
              ``scienceExposure is`` convolved

        Returns
        -------
        result : `lsst.pipe.base.Struct`
            An `lsst.pipe.base.Struct` containing these fields:

            - ``subtractedExposure`` : subtracted Exposure
                scienceExposure - (matchedImage + backgroundModel)
            - ``matchedImage`` : ``templateExposure`` after warping to match
                                 ``templateExposure`` (if doWarping true),
                                 and convolving with psfMatchingKernel
            - ``psfMatchingKernel`` : PSF matching kernel
            - ``backgroundModel`` : differential background model
            - ``kernelCellSet`` : SpatialCellSet used to determine PSF matching kernel
        """
        results = self.matchExposures(
            templateExposure=templateExposure,
            scienceExposure=scienceExposure,
            templateFwhmPix=templateFwhmPix,
            scienceFwhmPix=scienceFwhmPix,
            candidateList=candidateList,
            doWarping=doWarping,
            convolveTemplate=convolveTemplate
        )
        # Always inherit WCS and photocalib from science exposure
        subtractedExposure = afwImage.ExposureF(scienceExposure, deep=True)
        # Note, the decorrelation afterburner re-calculates the variance plane
        # from the variance planes of the original exposures.
        # That recalculation code must be in accordance with the
        # photometric level set here in ``subtractedMaskedImage``.
        if convolveTemplate:
            subtractedMaskedImage = subtractedExposure.maskedImage
            subtractedMaskedImage -= results.matchedExposure.maskedImage
            subtractedMaskedImage -= results.backgroundModel
        else:
            subtractedMaskedImage = subtractedExposure.maskedImage
            subtractedMaskedImage[:, :] = results.warpedExposure.maskedImage
            subtractedMaskedImage -= results.matchedExposure.maskedImage
            subtractedMaskedImage -= results.backgroundModel

            # Preserve polarity of differences
            subtractedMaskedImage *= -1

            # Place back on native photometric scale
            subtractedMaskedImage /= results.psfMatchingKernel.computeImage(
                afwImage.ImageD(results.psfMatchingKernel.getDimensions()), False)
            # We matched to the warped template
            subtractedExposure.setPsf(results.warpedExposure.getPsf())

        import lsstDebug
        display = lsstDebug.Info(__name__).display
        displayDiffIm = lsstDebug.Info(__name__).displayDiffIm
        maskTransparency = lsstDebug.Info(__name__).maskTransparency
        if not maskTransparency:
            maskTransparency = 0
        if display:
            afwDisplay.setDefaultMaskTransparency(maskTransparency)
        if display and displayDiffIm:
            disp = afwDisplay.Display(frame=lsstDebug.frame)
            disp.mtv(templateExposure, title="Template")
            lsstDebug.frame += 1
            disp = afwDisplay.Display(frame=lsstDebug.frame)
            disp.mtv(results.matchedExposure, title="Matched template")
            lsstDebug.frame += 1
            disp = afwDisplay.Display(frame=lsstDebug.frame)
            disp.mtv(scienceExposure, title="Science Image")
            lsstDebug.frame += 1
            disp = afwDisplay.Display(frame=lsstDebug.frame)
            disp.mtv(subtractedExposure, title="Difference Image")
            lsstDebug.frame += 1

        results.subtractedExposure = subtractedExposure
        return results

    @timeMethod
    def subtractMaskedImages(self, templateMaskedImage, scienceMaskedImage, candidateList,
                             templateFwhmPix=None, scienceFwhmPix=None):
        """Psf-match and subtract two MaskedImages.

        Do the following, in order:

        - PSF-match templateMaskedImage to scienceMaskedImage
        - Determine the differential background
        - Return the difference: scienceMaskedImage
            ((warped templateMaskedImage convolved with psfMatchingKernel) + backgroundModel)

        Parameters
        ----------
        templateMaskedImage : `lsst.afw.image.MaskedImage`
            MaskedImage to PSF-match to ``scienceMaskedImage``
        scienceMaskedImage : `lsst.afw.image.MaskedImage`
            Reference MaskedImage
        templateFwhmPix : `float`
            FWHM (in pixels) of the Psf in the template image (image to convolve)
        scienceFwhmPix : `float`
            FWHM (in pixels) of the Psf in the science image
        candidateList : `list`, optional
            A list of footprints/maskedImages for kernel candidates;
            if `None` then source detection is run.

            - Currently supported: list of Footprints or measAlg.PsfCandidateF

        Returns
        -------
        results : `lsst.pipe.base.Struct`
            An `lsst.pipe.base.Struct` containing these fields:

            - ``subtractedMaskedImage`` : ``scienceMaskedImage`` - (matchedImage + backgroundModel)
            - ``matchedImage`` : templateMaskedImage convolved with psfMatchingKernel
            - `psfMatchingKernel`` : PSF matching kernel
            - ``backgroundModel`` : differential background model
            - ``kernelCellSet`` : SpatialCellSet used to determine PSF matching kernel

        """
        if not candidateList:
            raise RuntimeError("Candidate list must be populated by makeCandidateList")

        results = self.matchMaskedImages(
            templateMaskedImage=templateMaskedImage,
            scienceMaskedImage=scienceMaskedImage,
            candidateList=candidateList,
            templateFwhmPix=templateFwhmPix,
            scienceFwhmPix=scienceFwhmPix,
        )

        subtractedMaskedImage = afwImage.MaskedImageF(scienceMaskedImage, True)
        subtractedMaskedImage -= results.matchedImage
        subtractedMaskedImage -= results.backgroundModel
        results.subtractedMaskedImage = subtractedMaskedImage

        import lsstDebug
        display = lsstDebug.Info(__name__).display
        displayDiffIm = lsstDebug.Info(__name__).displayDiffIm
        maskTransparency = lsstDebug.Info(__name__).maskTransparency
        if not maskTransparency:
            maskTransparency = 0
        if display:
            afwDisplay.setDefaultMaskTransparency(maskTransparency)
        if display and displayDiffIm:
            disp = afwDisplay.Display(frame=lsstDebug.frame)
            disp.mtv(subtractedMaskedImage, title="Subtracted masked image")
            lsstDebug.frame += 1

        return results

    def getSelectSources(self, exposure, sigma=None, doSmooth=True, idFactory=None):
        """Get sources to use for Psf-matching.

        This method runs detection and measurement on an exposure.
        The returned set of sources will be used as candidates for
        Psf-matching.

        Parameters
        ----------
        exposure : `lsst.afw.image.Exposure`
            Exposure on which to run detection/measurement
        sigma : `float`
            Detection threshold
        doSmooth : `bool`
            Whether or not to smooth the Exposure with Psf before detection
        idFactory :
            Factory for the generation of Source ids

        Returns
        -------
        selectSources :
            source catalog containing candidates for the Psf-matching
        """
        if idFactory:
            table = afwTable.SourceTable.make(self.selectSchema, idFactory)
        else:
            table = afwTable.SourceTable.make(self.selectSchema)
        mi = exposure.getMaskedImage()

        imArr = mi.getImage().getArray()
        maskArr = mi.getMask().getArray()
        miArr = np.ma.masked_array(imArr, mask=maskArr)
        try:
            fitBg = self.background.fitBackground(mi)
            bkgd = fitBg.getImageF(self.background.config.algorithm,
                                   self.background.config.undersampleStyle)
        except Exception:
            self.log.warning("Failed to get background model.  Falling back to median background estimation")
            bkgd = np.ma.median(miArr)

        # Take off background for detection
        mi -= bkgd
        try:
            table.setMetadata(self.selectAlgMetadata)
            detRet = self.selectDetection.run(
                table=table,
                exposure=exposure,
                sigma=sigma,
                doSmooth=doSmooth
            )
            selectSources = detRet.sources
            self.selectMeasurement.run(measCat=selectSources, exposure=exposure)
        finally:
            # Put back on the background in case it is needed down stream
            mi += bkgd
            del bkgd
        return selectSources

    def makeCandidateList(self, templateExposure, scienceExposure, kernelSize, candidateList=None):
        """Make a list of acceptable KernelCandidates.

        Accept or generate a list of candidate sources for
        Psf-matching, and examine the Mask planes in both of the
        images for indications of bad pixels

        Parameters
        ----------
        templateExposure : `lsst.afw.image.Exposure`
            Exposure that will be convolved
        scienceExposure : `lsst.afw.image.Exposure`
            Exposure that will be matched-to
        kernelSize : `float`
            Dimensions of the Psf-matching Kernel, used to grow detection footprints
        candidateList : `list`, optional
            List of Sources to examine. Elements must be of type afw.table.Source
            or a type that wraps a Source and has a getSource() method, such as
            meas.algorithms.PsfCandidateF.

        Returns
        -------
        candidateList : `list` of `dict`
            A list of dicts having a "source" and "footprint"
            field for the Sources deemed to be appropriate for Psf
            matching
        """
        if candidateList is None:
            candidateList = self.getSelectSources(scienceExposure)

        if len(candidateList) < 1:
            raise RuntimeError("No candidates in candidateList")

        listTypes = set(type(x) for x in candidateList)
        if len(listTypes) > 1:
            raise RuntimeError("Candidate list contains mixed types: %s" % [t for t in listTypes])

        if not isinstance(candidateList[0], afwTable.SourceRecord):
            try:
                candidateList[0].getSource()
            except Exception as e:
                raise RuntimeError(f"Candidate List is of type: {type(candidateList[0])} "
                                   "Can only make candidate list from list of afwTable.SourceRecords, "
                                   f"measAlg.PsfCandidateF or other type with a getSource() method: {e}")
            candidateList = [c.getSource() for c in candidateList]

        candidateList = diffimTools.sourceToFootprintList(candidateList,
                                                          templateExposure, scienceExposure,
                                                          kernelSize,
                                                          self.kConfig.detectionConfig,
                                                          self.log)
        if len(candidateList) == 0:
            raise RuntimeError("Cannot find any objects suitable for KernelCandidacy")

        return candidateList

    def makeKernelBasisList(self, targetFwhmPix=None, referenceFwhmPix=None,
                            basisDegGauss=None, basisSigmaGauss=None, metadata=None):
        """Wrapper to set log messages for
        `lsst.ip.diffim.makeKernelBasisList`.

        Parameters
        ----------
        targetFwhmPix : `float`, optional
            Passed on to `lsst.ip.diffim.generateAlardLuptonBasisList`.
            Not used for delta function basis sets.
        referenceFwhmPix : `float`, optional
            Passed on to `lsst.ip.diffim.generateAlardLuptonBasisList`.
            Not used for delta function basis sets.
        basisDegGauss : `list` of `int`, optional
            Passed on to `lsst.ip.diffim.generateAlardLuptonBasisList`.
            Not used for delta function basis sets.
        basisSigmaGauss : `list` of `int`, optional
            Passed on to `lsst.ip.diffim.generateAlardLuptonBasisList`.
            Not used for delta function basis sets.
        metadata : `lsst.daf.base.PropertySet`, optional
            Passed on to `lsst.ip.diffim.generateAlardLuptonBasisList`.
            Not used for delta function basis sets.

        Returns
        -------
        basisList: `list` of `lsst.afw.math.kernel.FixedKernel`
            List of basis kernels.
        """
        basisList = makeKernelBasisList(self.kConfig,
                                        targetFwhmPix=targetFwhmPix,
                                        referenceFwhmPix=referenceFwhmPix,
                                        basisDegGauss=basisDegGauss,
                                        basisSigmaGauss=basisSigmaGauss,
                                        metadata=metadata)
        if targetFwhmPix == referenceFwhmPix:
            self.log.info("Target and reference psf fwhms are equal, falling back to config values")
        elif referenceFwhmPix > targetFwhmPix:
            self.log.info("Reference psf fwhm is the greater, normal convolution mode")
        else:
            self.log.info("Target psf fwhm is the greater, deconvolution mode")

        return basisList

    def _adaptCellSize(self, candidateList):
        """NOT IMPLEMENTED YET.
        """
        return self.kConfig.sizeCellX, self.kConfig.sizeCellY

    def _buildCellSet(self, templateMaskedImage, scienceMaskedImage, candidateList):
        """Build a SpatialCellSet for use with the solve method.

        Parameters
        ----------
        templateMaskedImage : `lsst.afw.image.MaskedImage`
            MaskedImage to PSF-matched to scienceMaskedImage
        scienceMaskedImage : `lsst.afw.image.MaskedImage`
            Reference MaskedImage
        candidateList : `list`
            A list of footprints/maskedImages for kernel candidates;

            - Currently supported: list of Footprints or measAlg.PsfCandidateF

        Returns
        -------
        kernelCellSet : `lsst.afw.math.SpatialCellSet`
            a SpatialCellSet for use with self._solve
        """
        if not candidateList:
            raise RuntimeError("Candidate list must be populated by makeCandidateList")

        sizeCellX, sizeCellY = self._adaptCellSize(candidateList)

        # Object to store the KernelCandidates for spatial modeling
        kernelCellSet = afwMath.SpatialCellSet(templateMaskedImage.getBBox(),
                                               sizeCellX, sizeCellY)

        ps = pexConfig.makePropertySet(self.kConfig)
        # Place candidates within the spatial grid
        for cand in candidateList:
            if isinstance(cand, afwDetect.Footprint):
                bbox = cand.getBBox()
            else:
                bbox = cand['footprint'].getBBox()
            tmi = afwImage.MaskedImageF(templateMaskedImage, bbox)
            smi = afwImage.MaskedImageF(scienceMaskedImage, bbox)

            if not isinstance(cand, afwDetect.Footprint):
                if 'source' in cand:
                    cand = cand['source']
            xPos = cand.getCentroid()[0]
            yPos = cand.getCentroid()[1]
            cand = diffimLib.makeKernelCandidate(xPos, yPos, tmi, smi, ps)

            self.log.debug("Candidate %d at %f, %f", cand.getId(), cand.getXCenter(), cand.getYCenter())
            kernelCellSet.insertCandidate(cand)

        return kernelCellSet

    def _validateSize(self, templateMaskedImage, scienceMaskedImage):
        """Return True if two image-like objects are the same size.
        """
        return templateMaskedImage.getDimensions() == scienceMaskedImage.getDimensions()

    def _validateWcs(self, templateExposure, scienceExposure):
        """Return True if the WCS of the two Exposures have the same origin and extent.
        """
        templateWcs = templateExposure.getWcs()
        scienceWcs = scienceExposure.getWcs()
        templateBBox = templateExposure.getBBox()
        scienceBBox = scienceExposure.getBBox()

        # LLC
        templateOrigin = templateWcs.pixelToSky(geom.Point2D(templateBBox.getBegin()))
        scienceOrigin = scienceWcs.pixelToSky(geom.Point2D(scienceBBox.getBegin()))

        # URC
        templateLimit = templateWcs.pixelToSky(geom.Point2D(templateBBox.getEnd()))
        scienceLimit = scienceWcs.pixelToSky(geom.Point2D(scienceBBox.getEnd()))

        self.log.info("Template Wcs : %f,%f -> %f,%f",
                      templateOrigin[0], templateOrigin[1],
                      templateLimit[0], templateLimit[1])
        self.log.info("Science Wcs : %f,%f -> %f,%f",
                      scienceOrigin[0], scienceOrigin[1],
                      scienceLimit[0], scienceLimit[1])

        templateBBox = geom.Box2D(templateOrigin.getPosition(geom.degrees),
                                  templateLimit.getPosition(geom.degrees))
        scienceBBox = geom.Box2D(scienceOrigin.getPosition(geom.degrees),
                                 scienceLimit.getPosition(geom.degrees))
        if not (templateBBox.overlaps(scienceBBox)):
            raise RuntimeError("Input images do not overlap at all")

        if ((templateOrigin != scienceOrigin)
            or (templateLimit != scienceLimit)
                or (templateExposure.getDimensions() != scienceExposure.getDimensions())):
            return False
        return True


subtractAlgorithmRegistry = pexConfig.makeRegistry(
    doc="A registry of subtraction algorithms for use as a subtask in imageDifference",
)

subtractAlgorithmRegistry.register('al', ImagePsfMatchTask)
