import unittest
import numpy as np
import copy

import lsst.utils.tests
import lsst.afw.detection as afwDetection
import lsst.afw.geom as afwGeom
import lsst.afw.image as afwImage
import lsst.afw.math as afwMath
import lsst.afw.table as afwTable
import lsst.daf.base as dafBase
import lsst.geom as geom
import lsst.meas.algorithms as measAlg
from lsst.meas.base import SingleFrameMeasurementTask
import lsst.meas.extensions.piff.piffPsfDeterminer  # noqa


def psfVal(ix, iy, x, y, sigma1, sigma2, b):
    """Return the value at (ix, iy) of a double Gaussian
       (N(0, sigma1^2) + b*N(0, sigma2^2))/(1 + b)
       centered at (x, y)
    """
    dx, dy = x - ix, y - iy
    theta = np.radians(30)
    ab = 1.0/0.75  # axis ratio
    c, s = np.cos(theta), np.sin(theta)
    u, v = c*dx - s*dy, s*dx + c*dy

    return (np.exp(-0.5*(u**2 + (v*ab)**2)/sigma1**2)
            + b*np.exp(-0.5*(u**2 + (v*ab)**2)/sigma2**2))/(1 + b)


class SpatialModelPsfTestCase(lsst.utils.tests.TestCase):
    """A test case for SpatialModelPsf"""

    def measure(self, footprintSet, exposure):
        """Measure a set of Footprints, returning a SourceCatalog"""
        catalog = afwTable.SourceCatalog(self.schema)

        footprintSet.makeSources(catalog)

        self.measureSources.run(catalog, exposure)
        return catalog

    def setUp(self):
        config = SingleFrameMeasurementTask.ConfigClass()
        config.slots.apFlux = 'base_CircularApertureFlux_12_0'
        self.schema = afwTable.SourceTable.makeMinimalSchema()

        self.measureSources = SingleFrameMeasurementTask(
            self.schema, config=config
        )
        self.usePsfFlag = self.schema.addField("use_psf", type="Flag")

        width, height = 110, 301

        self.mi = afwImage.MaskedImageF(geom.ExtentI(width, height))
        self.mi.set(0)
        sd = 3  # standard deviation of image
        self.mi.getVariance().set(sd*sd)
        self.mi.getMask().addMaskPlane("DETECTED")

        self.ksize = 31  # size of desired kernel

        sigma1 = 1.75
        sigma2 = 2*sigma1

        self.exposure = afwImage.makeExposure(self.mi)
        self.exposure.setPsf(measAlg.DoubleGaussianPsf(self.ksize, self.ksize,
                                                       1.5*sigma1, 1, 0.1))
        cdMatrix = np.array([1.0, 0.0, 0.0, 1.0])
        cdMatrix.shape = (2, 2)
        wcs = afwGeom.makeSkyWcs(crpix=geom.PointD(0, 0),
                                 crval=geom.SpherePoint(0.0, 0.0, geom.degrees),
                                 cdMatrix=cdMatrix)
        self.exposure.setWcs(wcs)

        #
        # Make a kernel with the exactly correct basis functions.
        # Useful for debugging
        #
        basisKernelList = []
        for sigma in (sigma1, sigma2):
            basisKernel = afwMath.AnalyticKernel(
                self.ksize, self.ksize, afwMath.GaussianFunction2D(sigma, sigma)
            )
            basisImage = afwImage.ImageD(basisKernel.getDimensions())
            basisKernel.computeImage(basisImage, True)
            basisImage /= np.sum(basisImage.getArray())

            if sigma == sigma1:
                basisImage0 = basisImage
            else:
                basisImage -= basisImage0

            basisKernelList.append(afwMath.FixedKernel(basisImage))

        order = 1  # 1 => up to linear
        spFunc = afwMath.PolynomialFunction2D(order)

        exactKernel = afwMath.LinearCombinationKernel(basisKernelList, spFunc)
        exactKernel.setSpatialParameters(
            [[1.0, 0, 0],
             [0.0, 0.5*1e-2, 0.2e-2]]
        )

        rand = afwMath.Random()  # make these tests repeatable by setting seed

        im = self.mi.getImage()
        afwMath.randomGaussianImage(im, rand)  # N(0, 1)
        im *= sd                               # N(0, sd^2)

        xarr, yarr = [], []

        for x, y in [(20, 20), (60, 20),
                     (30, 35),
                     (50, 50),
                     (20, 90), (70, 160), (25, 265), (75, 275), (85, 30),
                     (50, 120), (70, 80),
                     (60, 210), (20, 210),
                     ]:
            xarr.append(x)
            yarr.append(y)

        for x, y in zip(xarr, yarr):
            dx = rand.uniform() - 0.5   # random (centered) offsets
            dy = rand.uniform() - 0.5

            k = exactKernel.getSpatialFunction(1)(x, y)
            b = (k*sigma1**2/((1 - k)*sigma2**2))

            flux = 80000*(1 + 0.1*(rand.uniform() - 0.5))
            I0 = flux*(1 + b)/(2*np.pi*(sigma1**2 + b*sigma2**2))
            for iy in range(y - self.ksize//2, y + self.ksize//2 + 1):
                if iy < 0 or iy >= self.mi.getHeight():
                    continue

                for ix in range(x - self.ksize//2, x + self.ksize//2 + 1):
                    if ix < 0 or ix >= self.mi.getWidth():
                        continue

                    II = I0*psfVal(ix, iy, x + dx, y + dy, sigma1, sigma2, b)
                    Isample = rand.poisson(II)
                    self.mi.image[ix, iy, afwImage.LOCAL] += Isample
                    self.mi.variance[ix, iy, afwImage.LOCAL] += II

        bbox = geom.BoxI(geom.PointI(0, 0), geom.ExtentI(width, height))
        self.cellSet = afwMath.SpatialCellSet(bbox, 100)

        self.footprintSet = afwDetection.FootprintSet(
            self.mi, afwDetection.Threshold(100), "DETECTED"
        )

        self.catalog = self.measure(self.footprintSet, self.exposure)

        for source in self.catalog:
            cand = measAlg.makePsfCandidate(source, self.exposure)
            self.cellSet.insertCandidate(cand)

    def setupDeterminer(self, exposure):
        """Setup the starSelector and psfDeterminer"""
        starSelectorClass = measAlg.sourceSelectorRegistry["objectSize"]
        starSelectorConfig = starSelectorClass.ConfigClass()
        starSelectorConfig.sourceFluxField = "base_GaussianFlux_instFlux"
        starSelectorConfig.badFlags = [
            "base_PixelFlags_flag_edge",
            "base_PixelFlags_flag_interpolatedCenter",
            "base_PixelFlags_flag_saturatedCenter",
            "base_PixelFlags_flag_crCenter",
        ]
        # Set to match when the tolerance of the test was set
        starSelectorConfig.widthStdAllowed = 0.5

        self.starSelector = starSelectorClass(config=starSelectorConfig)

        self.makePsfCandidates = measAlg.MakePsfCandidatesTask()

        psfDeterminerClass = measAlg.psfDeterminerRegistry["piff"]
        psfDeterminerConfig = psfDeterminerClass.ConfigClass()
        width, height = exposure.getMaskedImage().getDimensions()
        psfDeterminerConfig.spatialOrder = 1

        self.psfDeterminer = psfDeterminerClass(psfDeterminerConfig)

    def subtractStars(self, exposure, catalog, chi_lim=-1):
        """Subtract the exposure's PSF from all the sources in catalog"""
        mi, psf = exposure.getMaskedImage(), exposure.getPsf()

        subtracted = mi.Factory(mi, True)
        for s in catalog:
            xc, yc = s.getX(), s.getY()
            bbox = subtracted.getBBox(afwImage.PARENT)
            if bbox.contains(geom.PointI(int(xc), int(yc))):
                measAlg.subtractPsf(psf, subtracted, xc, yc)
        chi = subtracted.Factory(subtracted, True)
        var = subtracted.getVariance()
        np.sqrt(var.getArray(), var.getArray())  # inplace sqrt
        chi /= var

        chi_min = np.min(chi.getImage().getArray())
        chi_max = np.max(chi.getImage().getArray())
        print(chi_min, chi_max)

        if chi_lim > 0:
            self.assertGreater(chi_min, -chi_lim)
            self.assertLess(chi_max, chi_lim)

    def testPiffDeterminer(self):
        """Test the (Piff) psfDeterminer on subImages"""

        self.setupDeterminer(self.exposure)
        metadata = dafBase.PropertyList()

        stars = self.starSelector.run(self.catalog, exposure=self.exposure)
        psfCandidateList = self.makePsfCandidates.run(
            stars.sourceCat,
            exposure=self.exposure
        ).psfCandidates
        psf, cellSet = self.psfDeterminer.determinePsf(
            self.exposure,
            psfCandidateList,
            metadata,
            flagKey=self.usePsfFlag
        )
        self.exposure.setPsf(psf)

        self.assertEqual(len(psfCandidateList), metadata['numAvailStars'])
        self.assertEqual(sum(self.catalog['use_psf']), metadata['numGoodStars'])

        # Test how well we can subtract the PSF model
        self.subtractStars(self.exposure, self.catalog, chi_lim=5.6)

        # Test PiffPsf.computeBBox
        self.assertEqual(psf.computeBBox(), psf.computeKernelImage().getBBox())

        # Some roundtrips
        with lsst.utils.tests.getTempFilePath(".fits") as tmpFile:
            self.exposure.writeFits(tmpFile)
            fitsIm = afwImage.ExposureF(tmpFile)
            copyIm = copy.deepcopy(self.exposure)

            for newIm in [fitsIm, copyIm]:
                # Piff doesn't enable __eq__ for its results, so we just check
                # that some PSF images come out the same.
                for point in [
                    geom.Point2D(0, 0),
                    geom.Point2D(10, 100),
                    geom.Point2D(-200, 30),
                    geom.Point2D(float("nan"))  # "nullPoint"
                ]:
                    self.assertImagesAlmostEqual(
                        psf.computeImage(point),
                        newIm.getPsf().computeImage(point)
                    )
                # Also check using default position
                self.assertImagesAlmostEqual(
                    psf.computeImage(),
                    newIm.getPsf().computeImage()
                )


class TestMemory(lsst.utils.tests.MemoryTestCase):
    pass


def setup_module(module):
    lsst.utils.tests.init()


if __name__ == "__main__":
    lsst.utils.tests.init()
    unittest.main()