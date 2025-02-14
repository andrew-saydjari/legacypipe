from __future__ import print_function
import os
import warnings
import numpy as np
import fitsio
from tractor.splinesky import SplineSky
from tractor import PixelizedPsfEx, PixelizedPSF
from astrometry.util.fits import fits_table
from legacypipe.bits import DQ_BITS

import logging
logger = logging.getLogger('legacypipe.image')
def info(*args):
    from legacypipe.utils import log_info
    log_info(logger, args)
def debug(*args):
    from legacypipe.utils import log_debug
    log_debug(logger, args)

'''
Base class for handling the images we process.  These are all
processed by variants of the NOAO Community Pipeline (CP), so this
base class is pretty specific.
'''

def remap_dq_cp_codes(dq, ignore_codes=None):
    '''
    Some versions of the CP use integer codes, not bit masks.
    This converts them.

    1 = bad
    2 = no value (for remapped and stacked data)
    3 = saturated
    4 = bleed mask
    5 = cosmic ray
    6 = low weight
    7 = diff detect (multi-exposure difference detection from median)
    8 = long streak (e.g. satellite trail)
    '''
    if ignore_codes is None:
        ignore_codes = []
    dqbits = np.zeros(dq.shape, np.int16)

    # Some images (eg, 90prime//CP20160403/ksb_160404_103333_ood_g_v1-CCD1.fits)
    # around saturated stars have the core with value 3 (satur), surrounded by one
    # pixel of value 1 (bad), and then more pixels with value 4 (bleed).
    # Set the BAD ones to SATUR.
    from scipy.ndimage.morphology import binary_dilation
    dq[np.logical_and(dq == 1, binary_dilation(dq == 3))] = 3

    for code,bitname in [(1, 'badpix'),
                         (2, 'badpix'),
                         (3, 'satur'),
                         (4, 'bleed'),
                         (5, 'cr'),
                         (6, 'badpix'),
                         (7, 'trans'),
                         (8, 'trans'),
                         ]:
        if code in ignore_codes:
            continue
        dqbits[dq == code] |= DQ_BITS[bitname]
    return dqbits

def apply_amp_correction_northern(camera, band, expnum, ccdname, mjdobs,
                                  img, invvar, x0, y0):
    from pkg_resources import resource_filename
    dirname = resource_filename('legacypipe', 'data')
    fn = os.path.join(dirname, 'ampcorrections.fits')
    A = fits_table(fn)
    # Find relevant row -- camera, filter, ccdname, mjd_start, mjd_end,
    # And then multiple rows of:
    #   xlo, xhi, ylo, yhi -> dzp
    # that might overlap this image.
    I = np.flatnonzero([(cam.strip() == camera) and
                        (f.strip() == band) and
                        (ccd.strip() == ccdname) and
                        (not(np.isfinite(mjdstart)) or (mjdobs >= mjdstart)) and
                        (not(np.isfinite(mjdend  )) or (mjdobs <= mjdend))
                        for cam,f,ccd,mjdstart,mjdend
                        in zip(A.camera, A.filter, A.ccdname,
                               A.mjd_start, A.mjd_end)])
    info('Found', len(I), 'relevant rows in amp-corrections file.')
    if len(I) == 0:
        return
    if img is not None:
        H,W = img.shape
    else:
        H,W = invvar.shape
    # x0,y0 are integer pixel coords
    # x1,y1 are INCLUSIVE integer pixel coords
    x1 = x0 + W - 1
    y1 = y0 + H - 1

    debug_corr = False
    if debug_corr:
        count_corr = np.zeros((H,W), np.uint8)
        corr_map = np.zeros((H,W), np.float32)
        fitsio.write('amp-corr-image-before-%s-%s-%s.fits' % (camera, expnum, ccdname), img, clobber=True)

    for a in A[I]:
        # In the file, xhi,yhi are NON-inclusive.
        if a.xlo > x1 or a.xhi <= x0:
            continue
        if a.ylo > y1 or a.yhi <= y0:
            continue
        # Overlap!
        info('Found overlap: image x', x0, x1, 'and amp range', a.xlo, a.xhi-1,
              'and image y', y0, y1, 'and amp range', a.ylo, a.yhi-1)
        xstart = max(0, a.xlo - x0)
        xend   = min(W, a.xhi - x0)
        ystart = max(0, a.ylo - y0)
        yend   = min(H, a.yhi - y0)
        info('Range in image: x', xstart, xend, ', y', ystart, yend, '(with image size %i x %i)' % (W,H))
        scale = 10.**(0.4 * a.dzp)
        info('dzp', a.dzp, '-> scaling image by', scale)
        if img is not None:
            img   [ystart:yend, xstart:xend] *= scale
        if invvar is not None:
            invvar[ystart:yend, xstart:xend] /= scale**2

        if debug_corr:
            count_corr[ystart:yend, xstart:xend] += 1
            corr_map[ystart:yend, xstart:xend] = scale

    if debug_corr:
        assert(np.all(count_corr == 1))
        fitsio.write('amp-corr-image-after-%s-%s-%s.fits' % (camera, expnum, ccdname), img, clobber=True)
        fitsio.write('amp-corr-map-%s-%s-%s.fits' % (camera, expnum, ccdname), corr_map, clobber=True)

def estimate_sky_from_pixels(img):
    from scipy.stats import sigmaclip
    nsigma = 3.
    clip_vals,_,_ = sigmaclip(img, low=nsigma, high=nsigma)
    skymed= np.median(clip_vals)
    skystd= np.std(clip_vals)
    return skymed, skystd

class LegacySurveyImage(object):
    '''
    A base class containing common code for the images we handle.

    You probably shouldn't need to directly instantiate this class,
    but rather use the recipe described in the __init__ method.

    Objects of this class represent the metadata we have on an image,
    and are used to handle some of the details of going from an entry
    in the CCDs table to a tractor Image object.

    '''

    # this is defined here for testing purposes (to handle the small
    # images used in unit tests): box size for SplineSky model
    splinesky_boxsize = 1024

    def __init__(self, survey, ccd, image_fn=None, image_hdu=0):
        '''
        Create a new LegacySurveyImage object, from a LegacySurveyData object,
        and one row of a CCDs fits_table object.

        You may not need to instantiate this class directly, instead using
        survey.get_image_object():

            survey = LegacySurveyData()
            # targetwcs = ....
            # ccds = survey.ccds_touching_wcs(targetwcs, ccdrad=None)
            ccds = survey.get_ccds()
            im = survey.get_image_object(ccds[0])
            # which does the same thing as:
            im = DecamImage(survey, ccds[0])

        Or, if you have a Community Pipeline-processed input file and
        FITS HDU extension number:

            survey = LegacySurveyData()
            ccds = exposure_metadata([filename], hdus=[hdu])
            im = DecamImage(survey, ccds[0])

        Perhaps the most important method in this class is
        *get_tractor_image*.

        '''
        super(LegacySurveyImage, self).__init__()
        self.survey = survey
        self._fits = None
        self._primary_header = None
        self._image_header = None

        if ccd is None and image_fn is None:
            raise RuntimeError('Either "ccd" or "image_fn" must be set')

        if image_fn is not None:
            # Read metadata from image header.
            self.image_filename = image_fn
            self.imgfn = os.path.join(self.survey.get_image_dir(), image_fn)
            # before opening the file, check for a cached copy-- but
            # note that we reset self.imgfn below, so that compute_filenames()
            # can do its thing on the original filename.  We assume
            # check_for_cached_files() will be called after the constructor to pick
            # up all available cached files.
            self.imgfn = survey.check_cache(self.imgfn)

            primhdr = self.read_image_primary_header()
            self.band = self.get_band(primhdr)
            self.propid = self.get_propid(primhdr)
            self.expnum = self.get_expnum(primhdr)
            self.camera = self.get_camera(primhdr)
            self.mjdobs = self.get_mjd(primhdr)
            self.exptime = self.get_exptime(primhdr)
            namechange = {'date': 'procdate',}
            for key in ['HA', 'DATE', 'PLVER', 'PLPROCID']:
                val = primhdr.get(key)
                if isinstance(val, str):
                    val = val.strip()
                    if len(val) == 0:
                        raise ValueError('Empty header card: %s' % key)
                key = namechange.get(key.lower(), key.lower())
                key = key.replace('-', '_')
                setattr(self, key, val)

            # hdu, ccdname, width, height, pixscale
            self.hdu = image_hdu
            if image_hdu is not None:
                hdr = self.read_image_header(ext=image_hdu)
                # Parse ZNAXIS[12] / NAXIS[12] ?
                info = self.read_image_fits()[image_hdu].get_info()
                #print('Image info:', info)
                self.height,self.width = info['dims']
                self.hdu = info['hdunum'] - 1
                self.ccdname = self.get_ccdname(primhdr, hdr)
                self.pixscale = self.get_pixscale(primhdr, hdr)
                self.fwhm = self.get_fwhm(primhdr, hdr)
            else:
                self.ccdname = ''
            self.dq_hdu = self.hdu
            self.wt_hdu = self.hdu

            self.sig1 = 0.
            self.ccdzpt = 0.
            self.dradec = (0., 0.)
            # Reset!
            self.imgfn = os.path.join(self.survey.get_image_dir(), image_fn)

        else:
            # Get metadata from ccd table entry.
            # Note here that "image_filename" is the *relative* path (from image_dir),
            # while "imgfn" is the full path.
            imgfn = ccd.image_filename.strip()
            self.image_filename = imgfn
            self.imgfn = os.path.join(self.survey.get_image_dir(), imgfn)
            self.hdu     = ccd.image_hdu
            self.dq_hdu  = ccd.image_hdu
            self.wt_hdu  = ccd.image_hdu
            self.expnum  = ccd.expnum
            self.ccdname = ccd.ccdname.strip()
            self.band    = ccd.filter.strip()
            self.exptime = ccd.exptime
            self.camera  = ccd.camera.strip()
            self.fwhm    = ccd.fwhm
            self.propid  = ccd.propid
            self.mjdobs  = ccd.mjd_obs
            self.width   = ccd.width
            self.height  = ccd.height
            # In nanomaggies.
            self.sig1    = ccd.sig1
            # Use dummy values to accommodate old calibs (which will fail later
            # unless old-calibs-ok=True)
            try:
                self.plver = getattr(ccd, 'plver', 'xxx').strip()
            except:
                print('Failed to read PLVER header card as a string.  This probably means your python fitsio package is too old.')
                print('Try upgrading to version 1.0.5 or later.')
                raise
            self.procdate = getattr(ccd, 'procdate', 'xxxxxxx').strip()
            self.plprocid = getattr(ccd, 'plprocid', 'xxxxxxx').strip()

            # Photometric and astrometric zeropoints
            self.ccdzpt = ccd.ccdzpt
            self.dradec = (ccd.ccdraoff / 3600., ccd.ccddecoff / 3600.)

            # in arcsec/pixel
            self.pixscale = 3600. * np.sqrt(np.abs(ccd.cd1_1 * ccd.cd2_2 -
                                                   ccd.cd1_2 * ccd.cd2_1))

        self.compute_filenames()

        # Which Data Quality bits mark saturation?
        self.dq_saturation_bits = DQ_BITS['satur'] # | DQ_BITS['bleed']

        # Calib filenames
        calibdir = self.survey.get_calib_dir()
        imgdir = os.path.dirname(self.image_filename)
        basename = self.get_base_name()
        if len(self.ccdname):
            calname = basename + '-' + self.ccdname
        else:
            calname = basename
        self.name = calname
        self.sefn         = os.path.join(calibdir, 'se',           imgdir, basename, calname + '-se.fits')
        self.psffn        = os.path.join(calibdir, 'psfex-single', imgdir, basename, calname + '-psfex.fits')
        self.skyfn        = os.path.join(calibdir, 'sky-single',   imgdir, basename, calname + '-splinesky.fits')
        self.merged_psffn = os.path.join(calibdir, 'psfex',        imgdir, basename + '-psfex.fits')
        self.merged_skyfn = os.path.join(calibdir, 'sky',          imgdir, basename + '-splinesky.fits')
        self.old_merged_skyfns = [os.path.join(calibdir, imgdir, basename + '-splinesky.fits')]
        self.old_merged_psffns = [os.path.join(calibdir, imgdir, basename + '-psfex.fits')]
        # not used by this code -- here for the sake of legacyzpts/merge_calibs.py
        self.old_single_psffn = os.path.join(calibdir, imgdir, basename, calname + '-psfex.fits')
        self.old_single_skyfn = os.path.join(calibdir, imgdir, basename, calname + '-splinesky.fits')
        # for debugging purposes
        self.print_imgpath = '/'.join(self.imgfn.split('/')[-5:])

    # For pickling
    def __getstate__(self):
        # Can't pickle our cached _fits item.
        d = self.__dict__.copy()
        d['_fits'] = None
        return d

    def get_base_name(self):
        # Returns the base name to use for this Image object.  This is
        # used for calib paths, and is joined with the CCD name to
        # form the name of this Image object and for calib filenames.
        basename = os.path.basename(self.image_filename)
        ### HACK -- keep only the first dotted component of the base filename.
        # This allows, eg, create-testcase.py to use image filenames like BASE.N3.fits
        # with only a single HDU.
        basename = basename.split('.')[0]
        return basename

    def override_ccd_table_types(self):
        return {}

    def validate_version(self, *args, **kwargs):
        return validate_version(*args, **kwargs)

    def compute_filenames(self):
        # Compute data quality and weight-map filenames
        self.dqfn = self.imgfn.replace('_ooi_', '_ood_').replace('_oki_','_ood_')
        self.wtfn = self.imgfn.replace('_ooi_', '_oow_').replace('_oki_','_oow_')
        assert(self.dqfn != self.imgfn)
        assert(self.wtfn != self.imgfn)

    def get_extension_list(self, debug=False):
        F = self.read_image_fits()
        exts = []
        for f in F[1:]:
            exts.append(f.get_extname())
            if debug:
                break
        return exts

    def read_image_fits(self):
        '''
        Returns a fitsio.FITS object for this image file.
        '''
        if self._fits is not None:
            return self._fits
        self._fits = fitsio.FITS(self.imgfn)
        return self._fits

    def validate_image_data(self, mp=None):
        '''
        This checks for a relatively common type of corruption we see in
        the CP files, where the overall structure of the FITS files
        looks okay, but the data are corrupt so attempts to funpack
        uncompress them fail.  Test for this by just finding the list
        of expected extensions in the image file, and reading each of
        those exts in the image, weight, and dq maps.
        '''
        exts = self.get_extension_list()
        args = []
        for fn in [self.imgfn, self.wtfn, self.dqfn]:
            if fn is None:
                continue
            args.extend([(fn,ext) for ext in exts])
        if mp is None:
            for a in args:
                _read_one_ext(a)
        else:
            mp.map(_read_one_ext, args)

    def nominal_zeropoint(self, band):
        return self.zp0[band]

    def extinction(self, band):
        return self.k_ext[band]

    def calibration_good(self, primhdr):
        '''Did the CP processing succeed for this image?  If not, no need to process further.
        '''
        return primhdr.get('WCSCAL', '').strip().lower().startswith('success')

    def has_astrometric_calibration(self, ccd):
        return ccd.ccdnastrom > 0

    def get_photometric_calibrator_cuts(self, name, cat):
        '''Returns whether to keep sources in the *cat* of photometric calibration
        stars from, eg, Pan-STARRS1 or SDSS.
        '''
        if name == 'ps1':
            gicolor= cat.median[:,0] - cat.median[:,2]
            return ((cat.nmag_ok[:, 0] > 0) &
                    (cat.nmag_ok[:, 1] > 0) &
                    (cat.nmag_ok[:, 2] > 0) &
                    (gicolor > 0.4) &
                    (gicolor < 2.7))
        if name == 'sdss':
            return np.ones(len(cat), bool)
        raise RuntimeError('Unknown photometric calibration set: %s' % name)
    def photometric_calibrator_to_observed(self, name, cat):
        if name == 'ps1':
            from legacypipe.ps1cat import ps1cat
            colorterm = self.colorterm_ps1_to_observed(cat.median, self.band)
            ps1band = ps1cat.ps1band[self.band]
            return cat.median[:, ps1band] + np.clip(colorterm, -1., +1.)
        elif name == 'sdss':
            from legacypipe.ps1cat import sdsscat
            colorterm = self.colorterm_sdss_to_observed(cat.psfmag, self.band)
            band = sdsscat.sdssband[self.band]
            return cat.psfmag[:, band] + np.clip(colorterm, -1., +1.)
        else:
            raise RuntimeError('No photometric conversion from %s to DECam' % name)

    def colorterm_ps1_to_observed(self, cat, band):
        raise RuntimeError('Not implemented: generic colorterm_ps1_to_observed')
    def colorterm_sdss_to_observed(self, cat, band):
        raise RuntimeError('Not implemented: generic colorterm_sdss_to_observed')

    def get_radec_bore(self, primhdr):
        from astrometry.util.starutil_numpy import hmsstring2ra, dmsstring2dec
        # In some DECam exposures, RA,DEC are floating-point, but RA is in *decimal hours*.
        # In others, RA does not exist (eg CP/V4.8.2a/CP20160824/c4d_160825_062109_ooi_g_ls9.fits.fz)
        # Fall back to TELRA in that case.
        ra_bore = dec_bore = None
        if 'RA' in primhdr.keys():
            try:
                ra_bore = hmsstring2ra(primhdr['RA'])
                dec_bore = dmsstring2dec(primhdr['DEC'])
            except:
                pass
        if dec_bore is None and 'TELRA' in primhdr.keys():
            ra_bore = hmsstring2ra(primhdr['TELRA'])
            dec_bore = dmsstring2dec(primhdr['TELDEC'])
        if dec_bore is None:
            raise ValueError('Failed to parse RA or TELRA in primary header to get telescope boresight')
        return ra_bore, dec_bore

    def get_camera(self, primhdr):
        cam = primhdr['INSTRUME']
        cam = cam.lower()
        return cam

    def get_ccdname(self, primhdr, hdr):
        return hdr['EXTNAME'].strip().upper()

    def get_gain(self, primhdr, hdr):
        return primhdr['GAIN']

    def get_band(self, primhdr):
        band = primhdr['FILTER']
        band = band.split()[0]
        return band

    def get_propid(self, primhdr):
        return primhdr['PROPID']

    def get_airmass(self, primhdr, imghdr, ra, dec):
        airmass = primhdr.get('AIRMASS', None)
        if airmass is None:
            airmass = self.recompute_airmass(primhdr, ra, dec)
        return airmass

    def recompute_airmass(self, primhdr, ra, dec):
        site = self.get_site()
        if site is None:
            print('AIRMASS missing and site not defined.')
            return None
        print('Recomputing AIRMASS')
        from astropy.time import Time as apyTime
        from astropy.coordinates import SkyCoord, AltAz
        time = apyTime(self.mjdobs + 0.5*self.exptime/3600./24., format='mjd')
        coords = SkyCoord(ra, dec, unit='deg')
        altaz = coords.transform_to(AltAz(obstime=time, location=site))
        airmass = altaz.secz
        return airmass

    def get_site(self):
        return None

    def get_expnum(self, primhdr):
        return primhdr['EXPNUM']

    def get_fwhm(self, primhdr, imghdr):
        return imghdr.get('FWHM', np.nan)

    def get_mjd(self, primhdr):
        return primhdr.get('MJD-OBS')

    def get_exptime(self, primhdr):
        return primhdr.get('EXPTIME')

    def get_pixscale(self, primhdr, hdr):
        return 3600. * np.sqrt(np.abs(hdr['CD1_1'] * hdr['CD2_2'] -
                                      hdr['CD1_2'] * hdr['CD2_1']))

    # Used during zeropointing / annotation
    def get_cd_matrix(self, primhdr, hdr):
        return hdr['CD1_1'], hdr['CD1_2'], hdr['CD2_1'], hdr['CD2_2']

    # Used during zeropointing
    def scale_image(self, img):
        return img

    def scale_weight(self, img):
        return img

    def estimate_sig1(self, img, invvar, dq, primhdr, imghdr):
        mediv = np.median(invvar[(invvar > 0) * (dq == 0)])
        mediv = self.scale_weight(mediv)
        return (1. / np.sqrt(mediv)) / self.exptime

    def estimate_sky(self, img, invvar, dq, primhdr, imghdr):
        '''
        Returns a pixelized (or scalar) estimate of the sky background,
        plus the median sky and the 'skyrms' scatter around that.
        '''
        skymed, skyrms = estimate_sky_from_pixels(img)
        return skymed, skymed, skyrms

    def get_zeropoint(self, primhdr, hdr):
        '''
        If a LegacySurveyImage subclass already has a photometric
        zeropoint available, return it here to avoid having to fetch
        reference stars and fit them.  This also prevents astrometric
        offsets from being computed.
        '''
        return None

    def __str__(self):
        return self.name

    def __repr__(self):
        return str(self)

    def check_for_cached_files(self, survey):
        for key in self.get_cacheable_filename_variables():
            fn = getattr(self, key, None)
            #debug('Image: checking cache for variable', key, '->', fn)
            if fn is None:
                continue
            cfn = survey.check_cache(fn)
            #debug('Checking for cached', key, ':', fn, '->', cfn)
            if cfn != fn:
                print('Using cached', cfn)
                setattr(self, key, cfn)

    def get_cacheable_filename_variables(self):
        '''
        These are names of self.X variables that are filenames that
        could be cached.  These variable may be *overwritten* by the
        cache-checking function, hence this should only be used for
        read-only files (eg not calib files).
        '''
        return ['imgfn', 'dqfn', 'wtfn']

    def get_cacheable_filenames(self):
        '''
        These are additional filenames (eg, calib files) that could be cached.
        '''
        return [self.psffn, self.skyfn, self.merged_psffn, self.merged_skyfn]

    def get_good_image_slice(self, extent, get_extent=False):
        '''
        extent = None or extent = [x0,x1,y0,y1]

        If *get_extent* = True, returns the new [x0,x1,y0,y1] extent.

        Returns a new pair of slices, or *extent* if the whole image is good.
        '''
        gx0,gx1,gy0,gy1 = self.get_good_image_subregion()
        if gx0 is None and gx1 is None and gy0 is None and gy1 is None:
            return extent
        if extent is None:
            imh,imw = self.get_image_shape()
            extent = (0, imw, 0, imh)
        x0,x1,y0,y1 = extent
        if gx0 is not None:
            x0 = max(x0, gx0)
        if gy0 is not None:
            y0 = max(y0, gy0)
        if gx1 is not None:
            x1 = min(x1, gx1)
        if gy1 is not None:
            y1 = min(y1, gy1)
        if get_extent:
            return (x0,x1,y0,y1)
        return slice(y0,y1), slice(x0,x1)

    def get_good_image_subregion(self):
        '''
        Returns x0,x1,y0,y1 of the good region of this chip,
        or None if no cut should be applied to that edge; returns
        (None,None,None,None) if the whole chip is good.

        This cut is applied in addition to any masking in the mask or
        invvar map.
        '''
        return None,None,None,None

    def estimate_memory_required(self, radecpoly=None, mywcs=None):
        '''
        Returns an estimate in bytes of the memory required to store
        this image's get_tractor_image tim.
        '''
        if mywcs is None:
            wcs = self.get_wcs()
        else:
            wcs = mywcs
        x0,x1,y0,y1,slc = self.get_image_extent(wcs=wcs, radecpoly=radecpoly)
        H = y1-y0
        W = x1-x0
        npix = H*W
        # 4 for float image
        # 4 for float invvar
        # 2 for int16 dq
        return npix * (4 + 4 + 2)

    def get_tractor_image(self, slc=None, radecpoly=None,
                          gaussPsf=False, pixPsf=True, hybridPsf=True,
                          normalizePsf=True,
                          apodize=False,
                          readsky=True,
                          nanomaggies=True, subsky=True, tiny=10,
                          dq=True, invvar=True, pixels=True,
                          no_remap_invvar=False,
                          constant_invvar=False,
                          old_calibs_ok=False,
                          trim_edges=True):
        '''
        Returns a tractor.Image ("tim") object for this image.

        Options describing a subimage to return:

        - *slc*: y,x slice objects
        - *radecpoly*: numpy array, shape (N,2), RA,Dec polygon describing
            bounding box to select.
        - *trim_edges*: if True, drop fully masked rows and columns at the
            edge of the image.

        Options determining the PSF model to use:

        - *gaussPsf*: single circular Gaussian PSF based on header FWHM value.
        - *pixPsf*: pixelized PsfEx model.
        - *hybridPsf*: combo pixelized PsfEx + Gaussian approx.

        Options determining the units of the image:

        - *nanomaggies*: convert the image to be in units of NanoMaggies;
          *tim.zpscale* contains the scale value the image was divided by.

        - *subsky*: instantiate and subtract the initial sky model,
          leaving a constant zero sky model?

        '''
        import astropy.time
        from tractor.tractortime import TAITime
        from tractor.image import Image
        from tractor.basics import NanoMaggies, LinearPhotoCal

        get_dq = dq
        get_invvar = invvar
        primhdr = self.read_image_primary_header()

        for fn,kw in [(self.imgfn, dict(data=primhdr)), (self.wtfn, {}), (self.dqfn, {})]:
            if fn is None:
                continue
            if not self.validate_version(fn, 'primaryheader',
                                         self.expnum, self.plver, self.plprocid,
                                         cpheader=True, old_calibs_ok=old_calibs_ok, **kw):
                raise RuntimeError('Version validation failed for filename %s (PLVER/PLPROCID)' % fn)
        band = self.band
        wcs = self.get_wcs()

        orig_slc = slc
        x0,x1,y0,y1,slc = self.get_image_extent(wcs=wcs, slc=slc, radecpoly=radecpoly)
        if y1 - y0 < tiny or x1 - x0 < tiny:
            debug('Skipping tiny subimage (y %i to %i, x %i to %i)' % (y0, y1, x0, x1))
            debug('slice:', orig_slc, '->', slc, 'radecpoly', radecpoly)
            return None

        # Read image pixels
        if pixels:
            debug('Reading image slice:', slc)
            img,imghdr = self.read_image(header=True, slc=slc)
            self.check_image_header(imghdr)
        else:
            img = np.zeros((y1-y0, x1-x0), np.float32)
            imghdr = self.read_image_header()
        assert(np.all(np.isfinite(img)))

        # Read data-quality (flags) map and zero out the invvars of masked pixels
        dq = None
        if get_invvar:
            get_dq = True
        if get_dq:
            dq,dqhdr = self.read_dq(slc=slc, header=True)
            if dq is not None:
                dq = self.remap_dq(dq, dqhdr)
        # Read inverse-variance (weight) map
        if get_invvar:
            invvar = self.read_invvar(slc=slc, dq=dq)
        else:
            invvar = np.ones_like(img) * 1./self.sig1**2
        if np.all(invvar == 0.):
            debug('Skipping zero-invvar image')
            return None

        self.fix_saturation(img, dq, invvar, primhdr, imghdr, slc)

        template_meta = None
        if pixels:
            template = self.get_sky_template(slc=slc, old_calibs_ok=old_calibs_ok)
            if template is not None:
                debug('Subtracting sky template')
                # unpack
                template,template_meta = template
                img -= template

        # for create_testcase: omit remappings.
        if not no_remap_invvar:
            invvar = self.remap_invvar(invvar, primhdr, img, dq)

        # header 'FWHM' is in pixels
        psf_fwhm = self.get_fwhm(primhdr, imghdr)
        assert(psf_fwhm > 0)
        psf_sigma = psf_fwhm / 2.35

        # Ugly: occasionally the CP marks edge pixels with SATUR (and
        # nearby pixels with BLEED).  Convert connected blobs of
        # SATUR|BLEED pixels that are touching the left or right (not
        # top/botom) to EDGE.  An example of this is
        # mosaic-121450-CCD3-z at RA,Dec (261.4182, 58.8528).  Note
        # that here we're not demanding it be the full CCD edge; we're
        # checking our x0,x1 subregion, which is not ideal.
        # Here we're assuming the bleed direction is vertical.
        # This step is not redundant with the following trimming of
        # masked edge pixels because the SATUR|BLEED pixels in these
        # cases do not fill full columns, so they still cause issues
        # with source detection.
        if get_dq:
            from scipy.ndimage.measurements import label
            bits = DQ_BITS['satur'] | DQ_BITS['bleed']
            if np.any(dq[:,0] & bits) or np.any(dq[:,-1] & bits):
                blobmap,_ = label(dq & bits)
                badblobs = np.unique(np.append(blobmap[:,0], blobmap[:,-1]))
                badblobs = badblobs[badblobs != 0]
                #debug('Bad blobs:', badblobs)
                for bad in badblobs:
                    n = np.sum(blobmap == bad)
                    debug('Setting', n, 'edge SATUR|BLEED pixels to EDGE')
                    dq[blobmap == bad] = DQ_BITS['edge']

        if trim_edges:
            # Drop rows and columns at the image edges that are all masked.
            for y0_new in range(y0, y1):
                if not np.all(invvar[y0_new-y0,:] == 0):
                    break
            for y1_new in reversed(range(y0, y1)):
                if not np.all(invvar[y1_new-y0,:] == 0):
                    break
            for x0_new in range(x0, x1):
                if not np.all(invvar[:,x0_new-x0] == 0):
                    break
            for x1_new in reversed(range(x0, x1)):
                if not np.all(invvar[:,x1_new-x0] == 0):
                    break
            y1_new += 1
            x1_new += 1
            if x0_new != x0 or x1_new != x1 or y0_new != y0 or y1_new != y1:
                #debug('Old x0,x1', x0,x1, 'y0,y1', y0,y1)
                #debug('New x0,x1', x0_new,x1_new, 'y0,y1', y0_new,y1_new)
                if y1_new - y0_new < tiny or x1_new - x0_new < tiny:
                    debug('Skipping tiny subimage (after clipping masked edges)')
                    return None

                img    = img   [y0_new-y0 : y1_new-y0, x0_new-x0 : x1_new-x0]
                invvar = invvar[y0_new-y0 : y1_new-y0, x0_new-x0 : x1_new-x0]
                if get_dq:
                    dq = dq[y0_new-y0 : y1_new-y0, x0_new-x0 : x1_new-x0]
                x0,x1,y0,y1 = x0_new,x1_new,y0_new,y1_new
                slc = slice(y0,y1), slice(x0,x1)

        if readsky:
            sky = self.read_sky_model(slc=slc, primhdr=primhdr, imghdr=imghdr,
                                      old_calibs_ok=old_calibs_ok,
                                      template_meta=template_meta)
        else:
            from tractor.sky import ConstantSky
            sky = ConstantSky(0.)
        skymod = np.zeros_like(img)
        sky.addTo(skymod)
        midsky = np.median(skymod)
        orig_sky = sky
        if subsky:
            from tractor.sky import ConstantSky
            debug('Instantiating and subtracting sky model')
            if pixels:
                img -= skymod
            zsky = ConstantSky(0.)
            zsky.version = getattr(sky, 'version', '')
            zsky.plver = getattr(sky, 'plver', '')
            del skymod
            sky = zsky
            del zsky

        orig_zpscale = zpscale = NanoMaggies.zeropointToScale(self.ccdzpt)
        if nanomaggies:
            # Scale images to Nanomaggies
            img /= zpscale
            invvar = invvar * zpscale**2
            if not subsky:
                sky.scale(1./zpscale)
            zpscale = 1.

        if constant_invvar:
            assert(nanomaggies)
            invvar[invvar > 0] = 1./self.sig1**2

        if apodize and slc is not None:
            sy,sx = slc
            y0,y1 = sy.start, sy.stop
            x0,x1 = sx.start, sx.stop
            H,W = invvar.shape
            # Compute apodization ramps -- separately for x and y to
            # handle narrow images
            xx = np.linspace(-np.pi, np.pi, min(W,100))
            rampx = np.arctan(xx)
            rampx = (rampx - rampx.min()) / (rampx.max() - rampx.min())
            xx = np.linspace(-np.pi, np.pi, min(H,100))
            rampy = np.arctan(xx)
            rampy = (rampy - rampy.min()) / (rampy.max() - rampy.min())
            # bottom
            invvar[:len(rampy),:] *= rampy[:,np.newaxis]
            # left
            invvar[:,:len(rampx)] *= rampx[np.newaxis,:]
            # top
            invvar[-len(rampy):,:] *= rampy[::-1][:,np.newaxis]
            # right
            invvar[:,-len(rampx):] *= rampx[::-1][np.newaxis,:]

            if False:
                import pylab as plt
                plt.clf()
                plt.imshow(invvar, interpolation='nearest', origin='lower')
                plt.savefig('apodized-%i-%s.png' % (self.expnum, self.ccdname))

        if subsky:
            # Warn if the subtracted sky doesn't seem to work well
            # (can happen, eg, if sky calibration product is inconsistent with
            #  the data)
            imgmed = np.median(img[invvar>0])
            if np.abs(imgmed) > self.sig1:
                warnings.warn('image median is %.2f sigma away from zero for image %s!' % (imgmed / self.sig1, str(self)))

        if subsky:
            self.apply_amp_correction(img, invvar, x0, y0)

        # Convert MJD-OBS, in UTC, into TAI
        mjd_tai = astropy.time.Time(self.mjdobs, format='mjd', scale='utc').tai.mjd
        tai = TAITime(None, mjd=mjd_tai)

        # tractor WCS object
        twcs = self.get_tractor_wcs(wcs, x0, y0, primhdr=primhdr, imghdr=imghdr,
                                    tai=tai)

        psf = self.read_psf_model(x0, y0, gaussPsf=gaussPsf, pixPsf=pixPsf,
                                  hybridPsf=hybridPsf, normalizePsf=normalizePsf,
                                  psf_sigma=psf_sigma,
                                  w=x1 - x0, h=y1 - y0,
                                  old_calibs_ok=old_calibs_ok)

        tim = Image(img, invvar=invvar, wcs=twcs, psf=psf,
                    photocal=LinearPhotoCal(zpscale, band=band),
                    sky=sky, name=self.name + ' ' + band)
        assert(np.all(np.isfinite(tim.getInvError())))
        tim.band = band

        # HACK -- create a local PSF model to instantiate the PsfEx
        # model, which handles non-unit pixel scaling.
        fullpsf = tim.psf
        th,tw = tim.shape
        tim.psf = fullpsf.constantPsfAt(tw//2, th//2)
        tim.psfnorm = self.psf_norm(tim)
        # Galaxy-detection norm
        tim.galnorm = self.galaxy_norm(tim)
        #print('Galnorm:', tim.galnorm)
        if not (np.isfinite(tim.psfnorm) and np.isfinite(tim.galnorm)):
            # This can happen if there is something very wrong with the PSF model (NaNs, etc)
            warnings.warn('Bad (nan) psfnorm or galnorm for %s' % self)
            return None
        tim.psf = fullpsf

        tim.time = tai
        tim.slice = slc
        tim.zpscale = orig_zpscale
        ## orig_sky: the splinesky model, in image counts; divide by
        ## zpscale to get to nanomaggies.
        tim.origsky = orig_sky
        tim.midsky = midsky
        tim.sig1 = self.sig1
        tim.psf_fwhm = psf_fwhm
        tim.psf_sigma = psf_sigma
        tim.propid = self.propid
        tim.sip_wcs = wcs
        tim.x0,tim.y0 = int(x0),int(y0)
        tim.imobj = self
        tim.primhdr = primhdr
        tim.hdr = imghdr
        tim.plver = primhdr.get('PLVER','').strip()
        tim.plprocid = str(primhdr.get('PLPROCID','')).strip()
        tim.skyver = (getattr(sky, 'version', ''), getattr(sky, 'plver', ''))
        tim.psfver = (getattr(psf, 'version', ''), getattr(psf, 'plver', ''))
        tim.datasum = imghdr.get('DATASUM')
        tim.procdate = primhdr.get('DATE', '')
        if get_dq:
            tim.dq = dq
        tim.dq_saturation_bits = self.dq_saturation_bits
        subh,subw = tim.shape
        tim.subwcs = tim.sip_wcs.get_subimage(tim.x0, tim.y0, subw, subh)
        return tim

    def fix_saturation(self, img, dq, invvar, primhdr, imghdr, slc):
        pass

    def get_sky_template(self, slc=None, old_calibs_ok=False):
        return None

    def get_image_extent(self, wcs=None, slc=None, radecpoly=None):
        '''
        Returns x0,x1,y0,y1,slc
        '''
        imh,imw = self.get_image_shape()
        x0,y0 = 0,0
        x1 = x0 + imw
        y1 = y0 + imh

        # Clip to RA,Dec polygon?
        if slc is None and radecpoly is not None:
            from astrometry.util.miscutils import clip_polygon
            imgpoly = [(1,1),(1,imh),(imw,imh),(imw,1)]
            _,tx,ty = wcs.radec2pixelxy(radecpoly[:-1,0], radecpoly[:-1,1])
            tpoly = list(zip(tx,ty))
            clip = clip_polygon(imgpoly, tpoly)
            clip = np.array(clip)
            if len(clip) == 0:
                return 0,0,0,0,None
            # Convert from FITS to python image coords
            clip -= 1
            x0,y0 = np.floor(clip.min(axis=0)).astype(int)
            x1,y1 = np.ceil (clip.max(axis=0)).astype(int)
            x0 = min(max(x0, 0), imw-1)
            y0 = min(max(y0, 0), imh-1)
            x1 = min(max(x1, 0), imw-1)
            y1 = min(max(y1, 0), imh-1)
            slc = slice(y0,y1+1), slice(x0,x1+1)
        # Slice?
        if slc is not None:
            sy,sx = slc
            y0,y1 = sy.start, sy.stop
            x0,x1 = sx.start, sx.stop

        # Is part of this image bad?
        old_extent = (x0,x1,y0,y1)
        new_extent = self.get_good_image_slice((x0,x1,y0,y1), get_extent=True)
        if new_extent != old_extent:
            x0,x1,y0,y1 = new_extent
            debug('Applying good subregion of CCD: slice is', x0,x1,y0,y1)
            if x0 >= x1 or y0 >= y1:
                return 0,0,0,0,None
            slc = slice(y0,y1), slice(x0,x1)
        return x0,x1,y0,y1,slc

    def remap_invvar(self, invvar, primhdr, img, dq):
        return invvar

    # A function that can be called by a subclasser's remap_invvar() method,
    # if desired, to include the contribution from the source Poisson fluctuations
    def remap_invvar_shotnoise(self, invvar, primhdr, img, dq):
        debug('Remapping weight map for', self.name)
        const_sky = primhdr['SKYADU'] # e/s, Recommended sky level keyword from Frank
        expt = primhdr['EXPTIME'] # s
        with np.errstate(divide='ignore'):
            var_SR = 1./invvar # e/s
        var_Astro = np.abs(img - const_sky) / expt # e/s
        wt = 1./(var_SR + var_Astro) # s/e

        # Zero out NaNs and masked pixels
        wt[np.isfinite(wt) == False] = 0.
        wt[dq != 0] = 0.

        return wt

    # Default: do nothing.
    def apply_amp_correction(self, img, invvar, x0, y0):
        pass

    # A function that can be called by subclassers to apply a per-amp
    # zeropoint correction.
    def apply_amp_correction_northern(self, img, invvar, x0, y0):
        apply_amp_correction_northern(self.camera, self.band, self.expnum,
                                      self.ccdname, self.mjdobs,
                                      img, invvar, x0, y0)

    def check_image_header(self, imghdr):
        # check consistency between the CCDs table and the image header
        e = imghdr['EXTNAME'].upper()
        if e.strip() != self.ccdname.strip():
            warnings.warn('Expected header EXTNAME="%s" to match self.ccdname="%s", self.imgfn=%s' % (e.strip(), self.ccdname,self.imgfn))

    def psf_norm(self, tim, x=None, y=None):
        # PSF norm
        psf = tim.psf
        h,w = tim.shape
        if x is None:
            x = w//2
        if y is None:
            y = h//2
        patch = psf.getPointSourcePatch(x, y).patch
        psfnorm = np.sqrt(np.sum(patch**2))
        return psfnorm

    def galaxy_norm(self, tim, x=None, y=None):
        # Galaxy-detection norm
        from tractor.patch import ModelMask
        from legacypipe.survey import SimpleGalaxy
        from tractor.basics import NanoMaggies

        h,w = tim.shape
        band = tim.band
        if x is None:
            x = w//2
        if y is None:
            y = h//2
        pos = tim.wcs.pixelToPosition(x, y)
        gal = SimpleGalaxy(pos, NanoMaggies(**{band:1.}))
        S = 32
        mm = ModelMask(int(x-S), int(y-S), 2*S+1, 2*S+1)
        galmod = gal.getModelPatch(tim, modelMask=mm).patch
        #galmod = np.maximum(0, galmod)
        #galmod /= galmod.sum()
        galnorm = np.sqrt(np.sum(galmod**2))
        return galnorm

    def _read_fits(self, fn, hdu, slc=None, header=None, fitsobj=None, **kwargs):
        if slc is not None:
            if fitsobj is None:
                fitsobj = fitsio.FITS(fn)
            f = fitsobj[hdu]
            img = f[slc]
            if header:
                hdr = f.read_header()
                return (img,hdr)
            return img
        if fitsobj is not None:
            f = fitsobj[hdu]
            img = f.read(**kwargs)
            if header:
                hdr = f.read_header()
                return (img,hdr)
            return img
        return fitsio.read(fn, ext=hdu, header=header, **kwargs)

    def read_image(self, **kwargs):
        '''
        Reads the image file from disk.

        The image is read from FITS file self.imgfn HDU self.hdu.

        Parameters
        ----------
        slc : slice, optional
            2-dimensional slice of the subimage to read.
        header : boolean, optional
            Return the image header also, as tuple (image, header) ?

        Returns
        -------
        image : numpy array
            The image pixels.
        (image, header) : (numpy array, fitsio header)
            If `header = True`.
        '''
        debug('Reading image from', self.imgfn, 'hdu', self.hdu)
        fitsobj = self.read_image_fits()
        return self._read_fits(self.imgfn, self.hdu, fitsobj=fitsobj, **kwargs)

    def get_image_shape(self):
        '''
        Returns image shape H,W.
        '''
        return self.height, self.width

    @property
    def shape(self):
        '''
        Returns the full shape of the image, (H,W).
        '''
        return self.get_image_shape()

    def read_image_primary_header(self, **kwargs):
        '''
        Reads the FITS primary (HDU 0) header from self.imgfn.

        Returns
        -------
        primary_header : fitsio header
            The FITS header
        '''
        if self._primary_header is not None:
            return self._primary_header
        self._primary_header = self.read_image_fits()[0].read_header()
        return self._primary_header

    def read_image_header(self, **kwargs):
        '''
        Reads the FITS image header from self.imgfn HDU self.hdu.

        Returns
        -------
        header : fitsio header
            The FITS header
        '''
        if self._image_header is not None:
            return self._image_header
        print('Reading', self.imgfn, 'ext', self.hdu)
        self._image_header = self.read_image_fits()[self.hdu].read_header()
        return self._image_header

    def read_dq(self, **kwargs):
        '''
        Reads the Data Quality (DQ) mask image.
        '''
        debug('Reading data quality image', self.dqfn, 'ext', self.dq_hdu)
        dq = self._read_fits(self.dqfn, self.dq_hdu, **kwargs)
        return dq

    def remap_dq(self, dq, header):
        '''
        Called by get_tractor_image() to map the results from read_dq
        into a bitmask.
        '''
        return remap_dq_cp_codes(dq)

    def read_invvar(self, clip=True, clipThresh=0.1, dq=None, slc=None,
                    **kwargs):
        '''
        Reads the inverse-variance (weight) map image.
        '''
        debug('Reading weight map image', self.wtfn, 'ext', self.wt_hdu)
        invvar = self._read_fits(self.wtfn, self.wt_hdu, slc=slc, **kwargs)
        if dq is not None:
            invvar[dq != 0] = 0.

        if clip:
            fixed = False
            try:
                fixed = fix_weight_quantization(invvar, self.wtfn, self.hdu, slc)
            except Exception as e:
                print('Fix_weight_quantization bailed out on', self.wtfn,
                      'hdu', self.hdu, ':', e)

            if not fixed:
                # Clamp near-zero (incl negative!) weight to zero,
                # which arise due to fpack.
                if clipThresh > 0.:
                    thresh = clipThresh * np.median(invvar[invvar > 0])
                else:
                    thresh = 0.
                invvar[invvar < thresh] = 0

        # ALSO hack around an issue in some ls9 reprocessings where isolated pixels have
        # anomalously large values.  Eg
        # > imstat /global/cfs/cdirs/cosmo/work/legacysurvey/dr9/images/decam/CP/V4.8.2a/CP20160301/c4d_160302_035101_oow_g_ls9.fits.fz"[S31]"
        # Statistics of 2046 x 4094  image
        #   mean value    = 0.0235224
        #   minimum value = -14.2634
        #   maximum value = 1628.22
        if self.sig1 > 0:
            from tractor.basics import NanoMaggies
            zpscale = NanoMaggies.zeropointToScale(self.ccdzpt)
            fixedwt = 1. / (self.sig1 * zpscale)**2
            thresh = 1.3 * fixedwt
            n = np.sum(invvar > thresh)
            if n > 0:
                info('Clipping %i pixels with anomalously large oow values: max %g vs median %g' % (n, np.max(invvar), fixedwt))
                invvar[invvar > thresh] = fixedwt
        invvar[invvar < 0.] = 0.
        assert(np.all(np.isfinite(invvar)))
        return invvar

    def get_tractor_wcs(self, wcs, x0, y0, tai=None,
                        primhdr=None, imghdr=None):
        from legacypipe.survey import LegacySurveyWcs
        twcs = LegacySurveyWcs(wcs, tai)
        if x0 or y0:
            twcs.setX0Y0(x0,y0)
        return twcs

    def get_wcs(self, hdr=None):
        from astrometry.util.util import wcs_pv2sip_hdr
        # Make sure the PV-to-SIP converter samples enough points for small
        # images
        stepsize = 0
        if min(self.width, self.height) < 600:
            stepsize = min(self.width, self.height) / 10.;
        if hdr is None:
            hdr = self.read_image_header()
        wcs = wcs_pv2sip_hdr(hdr, stepsize=stepsize)
        # Correction: ccd ra,dec offsets from zeropoints/CCDs file
        dra,ddec = self.dradec
        # debug('Applying astrometric zeropoint:', (dra,ddec))
        r,d = wcs.get_crval()
        wcs.set_crval((r + dra / np.cos(np.deg2rad(d)), d + ddec))
        wcs.version = ''
        phdr = self.read_image_primary_header()
        wcs.plver = phdr.get('PLVER', '').strip()
        return wcs

    def read_sky_model(self, slc=None, old_calibs_ok=False,
                       template_meta=None, **kwargs):
        '''
        Reads the sky model, returning a Tractor Sky object.
        '''
        from tractor.utils import get_class_from_name
        tryfns = [self.survey.find_file('sky', img=self),
                  self.survey.find_file('sky-single', img=self),
                  ] + self.old_merged_skyfns
        Ti = None
        for fn in tryfns:
            if not os.path.exists(fn):
                continue
            T = fits_table(fn)
            I, = np.nonzero((T.expnum == self.expnum) *
                            np.array([c.strip() == self.ccdname
                                      for c in T.ccdname]))
            debug('Found', len(I), 'matching CCDs in merged sky file')
            if len(I) != 1:
                continue
            if not self.validate_version(
                    fn, 'table', self.expnum, self.plver, self.plprocid,
                    data=T, old_calibs_ok=old_calibs_ok):
                raise RuntimeError('Sky file %s did not pass consistency validation (PLVER, PLPROCID, EXPNUM)' % fn)
            Ti = T[I[0]]
        if Ti is None:
            raise RuntimeError('Failed to find sky model in files: %s' % ', '.join(tryfns))

        if template_meta is not None:
            # Check sky-template subtraction metadata!
            sver = getattr(Ti, 'templ_ver', -2)
            tver = template_meta.get('version', -3)
            srun = getattr(Ti, 'templ_run', -2)
            trun = template_meta.get('run', -3)
            sscale = getattr(Ti, 'templ_scale', -2)
            tscale = template_meta.get('scale', -3)
            if sver != tver or srun != trun or sscale != tscale:
                if old_calibs_ok:
                    warnings.warn('For image %s, Splinesky template version/run/scale %s/%s/%s'
                                  'does not match sky template %s/%s/%s, but old_calibs_ok is set' %
                                  (self, sver, srun, sscale, tver, trun, tscale))
                elif sver == -2 and srun == -2 and sscale == -2:
                    warnings.warn('For image %s, splinesky does not have sky-template version/run/scale values' % (self))
                else:
                    raise RuntimeError('Splinesky template version/run/scale %s/%s/%s does not match sky template %s/%s/%s, CCD %s' %
                                       (sver, srun, sscale, tver, trun, tscale, self.name))

        # Remove any padding
        h,w = Ti.gridh, Ti.gridw
        Ti.gridvals = Ti.gridvals[:h, :w]
        Ti.xgrid = Ti.xgrid[:w]
        Ti.ygrid = Ti.ygrid[:h]
        skyclass = Ti.skyclass.strip()

        if skyclass == 'tractor.splinesky.SplineSky':
            clazz = LegacySplineSky
        else:
            clazz = get_class_from_name(skyclass)
        fromfits = getattr(clazz, 'from_fits_row')
        sky = fromfits(Ti)
        if slc is not None:
            sy,sx = slc
            x0,y0 = sx.start,sy.start
            sky.shift(x0, y0)
        sky.version = Ti.legpipev
        sky.plver = getattr(Ti, 'plver', '')
        sky.plprocid = getattr(Ti, 'plprocid', '')
        sky.procdate = getattr(Ti, 'procdate', '')
        sky.sig1 = Ti.sig1
        if hasattr(Ti, 'imgdsum'):
            sky.datasum = Ti.imgdsum
        return sky

    def read_psf_model(self, x0, y0,
                       gaussPsf=False, pixPsf=False, hybridPsf=False,
                       normalizePsf=False, old_calibs_ok=False,
                       psf_sigma=1., w=0, h=0):
        assert(gaussPsf or pixPsf or hybridPsf)
        if gaussPsf:
            from tractor import GaussianMixturePSF
            v = psf_sigma**2
            psf = GaussianMixturePSF(1., 0., 0., v, v, 0.)
            debug('WARNING: using mock PSF:', psf)
            psf.version = '0'
            psf.plver = ''
            return psf

        # spatially varying pixelized PsfEx
        from tractor import PsfExModel
        tryfns = [self.survey.find_file('psf', img=self),
                  self.survey.find_file('psf-single', img=self)] + self.old_merged_psffns
        Ti = None
        header = None
        for fn in tryfns:
            if not os.path.exists(fn):
                continue
            T = fits_table(fn)
            header = T.get_header()
            I, = np.nonzero((T.expnum == self.expnum) *
                            np.array([c.strip() == self.ccdname
                                      for c in T.ccdname]))
            debug('Found', len(I), 'matching CCDs')
            if len(I) != 1:
                continue
            if not self.validate_version(
                    fn, 'table', self.expnum, self.plver, self.plprocid,
                    data=T, old_calibs_ok=old_calibs_ok):
                raise RuntimeError('Merged PSFEx file %s did not pass consistency validation (PLVER, PLPROCID, EXPNUM)' % fn)
            Ti = T[I[0]]
            break
        if Ti is None:
            raise RuntimeError('Failed to find PsfEx model in files: %s' % ', '.join(tryfns))
        if Ti.psf_samp == 0.0:
            raise RuntimeError('PsfEx failed: sampling (psf_samp) = 0 in file %s' % fn)
        # Remove any padding
        degree = Ti.poldeg1
        # number of terms in polynomial
        ne = (degree + 1) * (degree + 2) // 2
        Ti.psf_mask = Ti.psf_mask[:ne, :Ti.psfaxis1, :Ti.psfaxis2]

        assert(np.all(np.isfinite(Ti.psf_mask)))

        # If degree 0, set polname* to avoid assertion error in tractor
        if degree == 0:
            Ti.polname1 = 'X_IMAGE'
            Ti.polname2 = 'Y_IMAGE'
            Ti.polgrp1 = 1
            Ti.polgrp2 = 1
            Ti.polngrp = 1
        psfex = PsfExModel(Ti=Ti)

        if normalizePsf:
            debug('Normalizing PSF')
            psf = NormalizedPixelizedPsfEx(None, psfex=psfex)
        else:
            psf = PixelizedPsfEx(None, psfex=psfex)

        psf.version = Ti.legpipev.strip()
        psf.plver = getattr(Ti, 'plver', '')
        psf.procdate = getattr(Ti, 'procdate', '')
        psf.plprocid = getattr(Ti, 'plprocid', '')
        psf.datasum  = getattr(Ti, 'imgdsum', '')
        psf.fwhm = Ti.psf_fwhm
        psf.header = header

        psf.shift(x0, y0)
        if hybridPsf:
            from tractor.psf import HybridPixelizedPSF, NCircularGaussianPSF
            psf = HybridPixelizedPSF(psf, cx=w/2., cy=h/2.,
                                     gauss=NCircularGaussianPSF([psf.fwhm / 2.35], [1.]))
        debug('Using PSF model', psf)

        cols = Ti.get_columns()
        if 'moffat_alpha' in cols and 'moffat_beta' in cols:
            psf.moffat = (Ti.moffat_alpha, Ti.moffat_beta)
        return psf


    ######## Calibration tasks ###########


    def funpack_files(self, imgfn, maskfn, imghdu, maskhdu, todelete):
        '''Source Extractor can't handle .fz files, so unpack them.'''
        from legacypipe.survey import create_temp
        tmpimgfn = None
        tmpmaskfn = None
        # For FITS files that are not actually fpack'ed, funpack -E
        # fails.  Check whether actually fpacked.
        fcopy = False
        hdr = self.read_image_header()
        if not ((hdr['XTENSION'] == 'BINTABLE') and hdr.get('ZIMAGE', False)):
            debug('Image %s, HDU %i is not fpacked; just imcopying.' %
                  (imgfn,  imghdu))
            fcopy = True

        tmpimgfn  = create_temp(suffix='.fits')
        tmpmaskfn = create_temp(suffix='.fits')
        todelete.append(tmpimgfn)
        todelete.append(tmpmaskfn)

        if fcopy:
            cmd = 'imcopy %s"+%i" %s' % (imgfn, imghdu, tmpimgfn)
        else:
            cmd = 'funpack -E %i -O %s %s' % (imghdu, tmpimgfn, imgfn)
        debug(cmd)
        if os.system(cmd):
            raise RuntimeError('Command failed: ' + cmd)

        if fcopy:
            cmd = 'imcopy %s"+%i" %s' % (maskfn, maskhdu, tmpmaskfn)
        else:
            cmd = 'funpack -E %i -O %s %s' % (maskhdu, tmpmaskfn, maskfn)
        debug(cmd)
        if os.system(cmd):
            print('Command failed: ' + cmd)
            M,hdr = self._read_fits(maskfn, maskhdu, header=True)
            print('Read', M.dtype, M.shape)
            fitsio.write(tmpmaskfn, M, header=hdr, clobber=True)
            print('Wrote', tmpmaskfn, 'with fitsio')

        return tmpimgfn,tmpmaskfn

    def run_se(self, imgfn, maskfn):
        from astrometry.util.file import trymakedirs
        sedir = self.survey.get_se_dir()
        trymakedirs(self.sefn, dir=True)
        # We write the SE catalog to a temp file then rename, to avoid
        # partially-written outputs.
        tmpfn = os.path.join(os.path.dirname(self.sefn),
                             'tmp-' + os.path.basename(self.sefn))
        cmd = ' '.join([
            'sex',
            '-c', os.path.join(sedir, self.camera + '.se'),
            '-PARAMETERS_NAME', os.path.join(sedir, self.camera + '.param'),
            '-FILTER_NAME %s' % os.path.join(sedir, self.camera + '.conv'),
            '-FLAG_IMAGE %s' % maskfn,
            '-CATALOG_NAME %s' % tmpfn,
            '-VERBOSE_TYPE QUIET',
            imgfn])
        debug(cmd)
        rtn = os.system(cmd)
        if rtn:
            raise RuntimeError('Command failed: ' + cmd)
        os.rename(tmpfn, self.sefn)

    def run_psfex(self, git_version=None, ps=None):
        from astrometry.util.file import trymakedirs
        from legacypipe.survey import get_git_version
        sedir = self.survey.get_se_dir()
        trymakedirs(self.psffn, dir=True)
        primhdr = self.read_image_primary_header()
        plver = primhdr.get('PLVER', 'V0.0').strip()
        try:
            plprocid = str(primhdr['PLPROCID']).strip()
        except:
            plprocid = 'xxx'
        imghdr = self.read_image_header()
        datasum = imghdr.get('DATASUM', '0')
        procdate = primhdr.get('DATE', 'xxx')
        if git_version is None:
            git_version = get_git_version()
        # We write the PSF model to a .fits.tmp file, then rename to .fits
        psfdir = os.path.dirname(self.psffn)
        # This is the output filename that psfex will choose (since we tell it the PSF_SUFFIX)
        psftmpfn = os.path.join(psfdir, os.path.basename(self.sefn).replace('.fits','') + '.psf.tmp')
        psfexflags = self.survey.get_psfex_conf(self.camera,
                                                self.expnum, self.ccdname)
        cmd = 'psfex -c %s -PSF_DIR %s -PSF_SUFFIX .psf.tmp %s %s' % (os.path.join(sedir, self.camera + '.psfex'), psfdir, psfexflags, self.sefn)
        debug(cmd)
        rtn = os.system(cmd)
        if rtn:
            raise RuntimeError('Command failed: %s: return value: %i' % (cmd,rtn))

        # Convert into a "merged psfex" format file.
        T = psfex_single_to_merged(psftmpfn, self.expnum, self.ccdname)
        # add our own metadata values
        for k,v in [
                ('legpipev', git_version),
                ('plver',    plver),
                ('plprocid', plprocid),
                ('procdate', procdate),
                ('imgdsum',  datasum),
                ]:
            T.set(k, np.array([v]))

        psftmpfn2 = os.path.join(psfdir, os.path.basename(self.sefn).replace('.fits','') + '.psf.tmp2')
        T.writeto(psftmpfn2)
        os.remove(psftmpfn)
        os.rename(psftmpfn2, self.psffn)

    def run_sky(self, splinesky=True, git_version=None, ps=None, survey=None,
                gaia=True, release=0, survey_blob_mask=None,
                halos=True, subtract_largegalaxies=True):
        from scipy.ndimage.morphology import binary_dilation
        from astrometry.util.file import trymakedirs
        from astrometry.util.miscutils import estimate_mode

        plots = (ps is not None)

        slc = self.get_good_image_slice(None)
        img = self.read_image(slc=slc)
        dq,dqhdr = self.read_dq(slc=slc, header=True)
        if dq is not None:
            dq = self.remap_dq(dq, dqhdr)
        wt = self.read_invvar(slc=slc, dq=dq)
        primhdr = self.read_image_primary_header()
        imghdr = self.read_image_header()

        self.fix_saturation(img, dq, wt, primhdr, imghdr, slc)

        template_meta = {}
        template = self.get_sky_template(slc=slc)
        if template is not None:
            debug('Subtracting sky template before computing splinesky')
            # unpack
            template,template_meta = template
            img -= template

        plver = primhdr.get('PLVER', 'V0.0').strip()
        plprocid = str(primhdr.get('PLPROCID', '0')).strip()
        datasum = imghdr.get('DATASUM', '0')
        procdate = primhdr.get('DATE', '0')
        if git_version is None:
            from legacypipe.survey import get_git_version
            git_version = get_git_version()

        good = (wt > 0)
        if np.sum(good) == 0:
            from legacypipe.utils import ZeroWeightError
            raise ZeroWeightError('No pixels with weight > 0 in: ' + str(self))

        # Do a few different scalar sky estimates
        if np.sum(good) > 100:
            try:
                sky_mode = estimate_mode(img[good], raiseOnWarn=False)
            except:
                sky_mode = 0.
        else:
            sky_mode = 0.0
        if np.isnan(sky_mode) or np.isinf(sky_mode):
            sky_mode = 0.0

        sky_median = np.median(img[good])

        if not splinesky:
            #### This code branch has not been tested recently...
            from tractor.sky import ConstantSky
            if sky_mode != 0.:
                skyval = sky_mode
                skymeth = 'mode'
            else:
                skyval = sky_median
                skymeth = 'median'
            tsky = ConstantSky(skyval)
            primhdr.add_record(dict(name='SKYMETH', value=skymeth,
                                comment='estimate_mode, or fallback to median?'))
            sig1 = 1./np.sqrt(np.median(wt[wt>0]))
            masked = (img - skyval) > (5.*sig1)
            masked = binary_dilation(masked, iterations=3)
            masked[wt == 0] = True
            primhdr.add_record(dict(name='SIG1', value=sig1,
                                comment='Median stdev of unmasked pixels'))
            trymakedirs(self.skyfn, dir=True)
            tmpfn = os.path.join(os.path.dirname(self.skyfn),
                             'tmp-' + os.path.basename(self.skyfn))
            tsky.write_fits(tmpfn, hdr=primhdr)
            os.rename(tmpfn, self.skyfn)
            debug('Wrote sky model', self.skyfn)
            return

        # Splinesky
        from scipy.ndimage.filters import uniform_filter
        from scipy.stats import sigmaclip

        sig1 = 1./np.sqrt(np.median(wt[good]))
        cimage,_,_ = sigmaclip(img[good], low=2.0, high=2.0)
        sky_clipped_median = np.median(cimage)

        # from John (adapted):
        # Smooth by a boxcar filter before cutting pixels above threshold --
        boxcar = 5
        # Sigma of boxcar-smoothed image
        bsig1 = sig1 / boxcar
        masked = np.abs(uniform_filter(img - sky_clipped_median, size=boxcar,
                                       mode='constant')) > (3.*bsig1)
        masked = binary_dilation(masked, iterations=3)
        if np.sum(good * (masked==False)) > 100:
            cimage, _, _ = sigmaclip(img[good * (masked==False)],
                                     low=2.0, high=2.0)
            if len(cimage) > 0:
                sky_john = np.median(cimage)
            else:
                sky_john = 0.0
            del cimage
        else:
            debug('Too few good pixels to estimate sky_john')
            sky_john = 0.0

        boxsize = self.splinesky_boxsize

        # Initial scalar sky estimate; also the fallback value if
        # everything is masked in one of the splinesky grid cells.
        initsky = sky_john
        if initsky == 0.0:
            initsky = sky_clipped_median

        # For DECam chips where we drop half the chip, spline becomes
        # underconstrained
        if min(img.shape) / boxsize < 4:
            boxsize /= 2

        # Compute initial model...
        skyobj = SplineSky.BlantonMethod(img - initsky, good, boxsize,
                                         min_fraction=0.25)
        skymod = np.zeros_like(img)
        skyobj.addTo(skymod)

        # Now mask bright objects in a boxcar-smoothed (image -
        # initial sky model) Smooth by a boxcar filter before cutting
        # pixels above threshold --
        boxcar = 5
        # Sigma of boxcar-smoothed image
        bsig1 = sig1 / boxcar
        masked = np.abs(uniform_filter(img - initsky - skymod,
                                       size=boxcar, mode='constant')
                        > (3.*bsig1))
        masked = binary_dilation(masked, iterations=3)
        good[masked] = False

        # Also mask based on reference stars and galaxies.
        from legacypipe.reference import get_reference_sources
        from legacypipe.reference import get_galaxy_sources
        from legacypipe.reference import get_reference_map
        wcs = self.get_wcs(hdr=imghdr)
        debug('Good image slice:', slc)
        x0 = y0 = 0
        if slc is not None:
            sy,sx = slc
            y0,y1 = sy.start, sy.stop
            x0,x1 = sx.start, sx.stop
            wcs = wcs.get_subimage(x0, y0, int(x1-x0), int(y1-y0))
        # Grab reference sources
        refs,_ = get_reference_sources(survey, wcs, self.pixscale, None,
                                       tycho_stars=True, gaia_stars=gaia,
                                       large_galaxies=True,
                                       star_clusters=True,
                                       clean_columns=False)
        refgood = (get_reference_map(wcs, refs) == 0)

        sub_sga_version = '  '
        sub_galaxies = None
        if subtract_largegalaxies:
            from legacypipe.reference import get_large_galaxy_version
            galfn = survey.find_file('large-galaxies')
            debug('Large-galaxies filename:', galfn)
            if galfn is None:
                subtract_largegalaxies = False
        if subtract_largegalaxies:
            sub_sga_version,_ = get_large_galaxy_version(galfn)
            debug('SGA version:', sub_sga_version)
            debug('Large galaxies:', np.sum(refs.islargegalaxy))
            debug('Freezeparams:', np.sum(refs.islargegalaxy * refs.freezeparams))
            # we only want to subtract pre-burned, frozen galaxies.
            I = np.flatnonzero(refs.islargegalaxy * refs.freezeparams)
            info('Found', len(I), 'SGA galaxies to subtract before sky')
            if len(I):
                sub_galaxies = get_galaxy_sources(refs[I], [self.band])
        galmod = None
        if sub_galaxies is not None:
            from tractor import (ConstantSky, ConstantFitsWcs, NanoMaggies,
                                 LinearPhotoCal, Image, Tractor)
            info('Subtracting %i SGA galaxies before estimating sky' % len(sub_galaxies))
            for g in sub_galaxies:
                debug('  ', g)
            psf_fwhm = self.get_fwhm(primhdr, imghdr)
            assert(psf_fwhm > 0)
            psf_sigma = psf_fwhm / 2.35
            psf = self.read_psf_model(x0, y0, pixPsf=True, hybridPsf=True,
                                      normalizePsf=True, psf_sigma=psf_sigma)
            fakesky = ConstantSky(0.)
            twcs = ConstantFitsWcs(wcs)
            assert(self.ccdzpt > 0)
            zpscale = NanoMaggies.zeropointToScale(self.ccdzpt)
            # create tractor Image to render galaxy model.  The "img" element is
            # not used, but has the correct shape / type.
            tim = Image(img, wcs=twcs, psf=psf, sky=fakesky,
                        photocal=LinearPhotoCal(zpscale, band=self.band))
            tr = Tractor([tim], sub_galaxies)
            galmod = tr.getModelImage(0)
            # we set zpscale, so model image is in ADU.
            debug('Using zeropoint:', self.ccdzpt, 'to scale galaxy model by', zpscale)
            img -= galmod
            if not plots:
                del galmod

        haloimg = None
        halozpt = 0.
        if halos and self.camera == 'decam':
            # Subtract halos from Gaia stars.
            # "refs.donotfit" are Gaia sources that are near SGA galaxies.
            Igaia, = np.nonzero(refs.isgaia * refs.pointsource *
                                np.logical_not(refs.donotfit))
            if len(Igaia):
                info('Subtracting %i Gaia halos before estimating sky' % len(Igaia))
                from legacypipe.halos import decam_halo_model
                # moffat=True: include inner Moffat component in star halos.
                moffat = True
                haloimg = decam_halo_model(refs[Igaia], self.mjdobs, wcs,
                                           self.pixscale, self.band, self, moffat)
                # "haloimg" is in nanomaggies.  Convert to ADU via zeropoint...
                from tractor.basics import NanoMaggies
                assert(self.ccdzpt > 0)
                zpscale = NanoMaggies.zeropointToScale(self.ccdzpt)
                haloimg *= zpscale
                print('Using zeropoint:', self.ccdzpt, 'to scale halo image by', zpscale)
                halozpt = self.ccdzpt
                img -= haloimg
                if plots:
                    # Also compute halo image without Moffat component
                    nomoffhalo = decam_halo_model(refs[Igaia], self.mjdobs, wcs,
                        self.pixscale, self.band, self, False)
                    nomoffhalo *= zpscale
                    moffhalo = haloimg - nomoffhalo
                    del nomoffhalo
                if not plots:
                    del haloimg

        if plots:
            boxcargood = good.copy()

        blobmasked = False
        if survey_blob_mask is not None:
            # Read DR8 blob maps for all overlapping bricks and project them
            # into this CCD's pixel space.
            from legacypipe.survey import bricks_touching_wcs, wcs_for_brick
            from astrometry.util.resample import resample_with_wcs, OverlapError

            bricks = bricks_touching_wcs(wcs, survey=survey_blob_mask)
            H,W = wcs.shape
            allblobs = np.zeros((int(H),int(W)), bool)
            for brick in bricks:
                fn = survey_blob_mask.find_file('blobmap',brick=brick.brickname)
                if not os.path.exists(fn):
                    print('Warning: blob map for brick', brick.brickname,
                          'does not exist:', fn)
                    continue
                blobs = fitsio.read(fn)
                blobs = (blobs >= 0)
                brickwcs = wcs_for_brick(brick)
                try:
                    Yo,Xo,Yi,Xi,_ = resample_with_wcs(wcs, brickwcs)
                except OverlapError:
                    continue
                allblobs[Yo,Xo] |= blobs[Yi,Xi]
            ng = np.sum(good)
            if plots:
                blobgood = np.logical_not(allblobs)
            good[allblobs] = False
            del allblobs
            info('Masked', ng-np.sum(good), 'additional CCD pixels from blob maps')
            blobmasked = True

        # Now find the final sky model using that more extensive mask
        skyobj = SplineSky.BlantonMethod(img - initsky, good*refgood, boxsize,
                                         min_fraction=0.25)
        # add the initial sky estimate back in
        skyobj.offset(initsky)

        # Compute stats on sky
        skypix = np.zeros_like(img)
        skyobj.addTo(skypix)

        pcts = [0,10,20,30,40,50,60,70,80,90,100]
        pctpix = (img - skypix)[good * refgood]
        if len(pctpix):
            assert(np.all(np.isfinite(img[good * refgood])))
            assert(np.all(np.isfinite(skypix[good * refgood])))
            assert(np.all(np.isfinite(pctpix)))
            pctvals = np.percentile((img - skypix)[good * refgood], pcts)
        else:
            pctvals = [0] * len(pcts)
        H,W = img.shape
        fmasked = float(np.sum((good * refgood) == 0)) / (H*W)

        # DEBUG -- compute a splinesky on a finer grid and compare it.
        fineskyobj = SplineSky.BlantonMethod(img - initsky, good * refgood,
                                             boxsize//2,
                                             min_fraction=0.25)
        fineskyobj.offset(initsky)
        fineskyobj.addTo(skypix, -1.)
        fine_rms = np.sqrt(np.mean(skypix**2))

        if plots:
            import pylab as plt
            ima = dict(interpolation='nearest', origin='lower',
                       vmin=initsky-2.*sig1, vmax=initsky+5.*sig1, cmap='gray')
            ima2 = dict(interpolation='nearest', origin='lower',
                        vmin=initsky-0.5*sig1,vmax=initsky+0.5*sig1,cmap='gray')

            plt.clf()
            plt.imshow(img, **ima)
            plt.title('Image %s-%i-%s %s' % (self.camera, self.expnum,
                                             self.ccdname, self.band))
            ps.savefig()

            if galmod is not None:
                plt.clf()
                plt.imshow(img.T + galmod.T, **ima)
                plt.title('Image with SGA galaxies')
                ps.savefig()

                plt.clf()
                plt.imshow(initsky + galmod.T, **ima)
                plt.title('SGA galaxies subtracted')
                ps.savefig()

            if haloimg is not None:
                plt.clf()
                plt.imshow(img + haloimg, **ima)
                plt.title('Image with star halos')
                ps.savefig()

                plt.clf()
                imx = dict(interpolation='nearest', origin='lower',
                           vmin=-2*sig1,vmax=+2*sig1,cmap='gray')
                plt.imshow(haloimg, **imx)
                plt.title('Star halos')
                ps.savefig()

                plt.clf()
                imx = dict(interpolation='nearest', origin='lower',
                           vmin=-2*sig1,vmax=+2*sig1,cmap='gray')
                plt.imshow(moffhalo, **imx)
                plt.title('Moffat component of star halos')
                ps.savefig()

            plt.clf()
            plt.imshow(wt, interpolation='nearest', origin='lower',
                       cmap='gray')
            plt.title('Weight')
            ps.savefig()

            plt.clf()
            plt.subplot(2,1,1)
            plt.hist(wt.ravel(), bins=100)
            plt.xlabel('Invvar weights')
            plt.subplot(2,1,2)
            origwt = self._read_fits(self.wtfn, self.hdu, slc=slc)
            mwt = np.median(origwt[origwt>0])
            plt.hist(origwt.ravel(), bins=100, range=(-0.03 * mwt, 0.03 * mwt),
                     histtype='step', label='oow file', lw=3, alpha=0.3,
                     log=True)
            plt.hist(wt.ravel(), bins=100, range=(-0.03 * mwt, 0.03 * mwt),
                     histtype='step', label='clipped', log=True)
            plt.axvline(0.01 * mwt)
            plt.xlabel('Invvar weights')
            plt.legend()
            ps.savefig()

            from legacypipe.detection import plot_mask

            plt.clf()
            plt.imshow((img - initsky)*boxcargood + initsky, **ima)
            plot_mask(np.logical_not(boxcargood))
            plt.title('Image (boxcar masked)')
            ps.savefig()

            if survey_blob_mask is not None:
                plt.clf()
                plt.imshow((img - initsky)*blobgood + initsky, **ima)
                plot_mask(np.logical_not(blobgood))
                plt.title('Image (blob masked)')
                ps.savefig()

            plt.clf()
            plt.imshow((img - initsky)*refgood + initsky, **ima)
            plot_mask(np.logical_not(refgood))
            plt.title('Image (reference masked)')
            ps.savefig()

            plt.clf()
            plt.imshow((img - initsky)*(refgood * good) + initsky, **ima)
            plot_mask(np.logical_not(refgood * good))
            plt.title('Image (all masked)')
            ps.savefig()

            ax = plt.axis()
            for x in skyobj.xgrid:
                # We transpose the image!
                #plt.axvline(x, color='r')
                plt.axhline(x, color='r')
            for y in skyobj.ygrid:
                #plt.axhline(y, color='r')
                plt.axvline(y, color='r')
            plt.axis(ax)
            ps.savefig()

            plt.clf()
            plt.hist((img[good * refgood] - initsky).ravel(), bins=50)
            plt.title('Unmasked pixels')
            ps.savefig()

            gridvals = skyobj.spl(skyobj.xgrid, skyobj.ygrid) - initsky
            plt.clf()
            plt.imshow(gridvals,
                       interpolation='nearest', origin='lower',
                       vmin=-2.*sig1, vmax=+5.*sig1, cmap='gray')
            plt.colorbar()
            plot_mask(gridvals == 0)
            plt.title('Splinesky grid values')
            ps.savefig()

            plt.clf()
            plt.imshow(gridvals,
                       interpolation='nearest', origin='lower',
                       vmin=-0.5*sig1, vmax=+0.5*sig1, cmap='gray')
            plt.colorbar()
            plt.title('Splinesky grid values')
            ps.savefig()

            skypix = np.zeros_like(img)
            skyobj.addTo(skypix)
            plt.clf()
            plt.imshow(skypix, **ima2)
            plt.title('Sky model')
            ps.savefig()

            skypix2 = np.zeros_like(img)
            fineskyobj.addTo(skypix2)
            plt.clf()
            plt.imshow(skypix2, **ima2)
            plt.title('Fine sky model')
            ps.savefig()

        if slc is not None:
            sy,sx = slc
            y0 = sy.start
            x0 = sx.start
            skyobj.shift(-x0, -y0)

        T = skyobj.to_fits_table()
        for k,v in [('expnum', self.expnum),
                    ('ccdname', self.ccdname),
                    ('legpipev', git_version),
                    ('plver',    plver),
                    ('plprocid', plprocid),
                    ('procdate', procdate),
                    ('imgdsum',  datasum),
                    ('sig1', sig1),
                    ('templ_ver', template_meta.get('version', -1)),
                    ('templ_run', template_meta.get('run', -1)),
                    ('templ_scale', template_meta.get('scale', 0.)),
                    ('halo_zpt', halozpt),
                    ('blob_masked', blobmasked),
                    ('sub_sga_ver', sub_sga_version),
                    ('sky_mode', sky_mode),
                    ('sky_med', sky_median),
                    ('sky_cmed', sky_clipped_median),
                    ('sky_john', sky_john),
                    ('sky_fine', fine_rms),
                    ('sky_fmasked', fmasked),
                    ] + [('sky_p%i' % p, v) for p,v in zip(pcts, pctvals)]:
            T.set(k, np.array([v]))

        trymakedirs(self.skyfn, dir=True)
        tmpfn = os.path.join(os.path.dirname(self.skyfn),
                         'tmp-' + os.path.basename(self.skyfn))
        T.writeto(tmpfn)
        os.rename(tmpfn, self.skyfn)
        debug('Wrote sky model', self.skyfn)

    def run_calibs(self, psfex=True, sky=True, se=False,
                   fcopy=False, use_mask=True,
                   force=False, git_version=None,
                   splinesky=True, ps=None, survey=None,
                   gaia=True, old_calibs_ok=False,
                   survey_blob_mask=None, halos=True,
                   subtract_largegalaxies=True):
        '''
        Run calibration pre-processing steps.
        '''
        if psfex and not force:
            # Check whether PSF model already exists
            try:
                self.read_psf_model(0, 0, pixPsf=True, hybridPsf=True,
                                    old_calibs_ok=old_calibs_ok)
                psfex = False
            except Exception as e:
                debug('Did not find existing PsfEx model for', self, ':', e)

        if psfex:
            se = True

        # Don't need to run source extractor if the catalog file already exists
        if se and os.path.exists(self.sefn) and (not force):
            se = False

        if sky and not force:
            # Check whether sky model already exists
            try:
                self.read_sky_model(old_calibs_ok=old_calibs_ok)
                sky = False
            except Exception as e:
                debug('Did not find existing sky model for', self, ':', e)

        if se:
            # The image & mask files to process (funpacked if necessary)
            todelete = []
            imgfn,maskfn = self.funpack_files(self.imgfn, self.dqfn,
                                              self.hdu, self.dq_hdu, todelete)
            self.run_se(imgfn, maskfn)
            for fn in todelete:
                os.unlink(fn)

        psfexc = None
        skyexc = None
        if psfex:
            try:
                self.run_psfex(git_version=git_version, ps=ps)
            except Exception as ex:
                psfexc = ex
        if sky:
            try:
                self.run_sky(splinesky=splinesky, git_version=git_version, ps=ps, survey=survey, gaia=gaia, survey_blob_mask=survey_blob_mask, halos=halos, subtract_largegalaxies=subtract_largegalaxies)
            except Exception as ex:
                skyexc = ex
        if psfexc is not None:
            raise psfexc
        if skyexc is not None:
            raise skyexc

def _read_one_ext(args):
    fn,ext = args
    fitsio.read(fn, ext=ext)

def psfex_single_to_merged(infn, expnum, ccdname):
    # returns table T
    T = fits_table(infn)
    hdr = T.get_header()
    for k,v in [
            ('expnum',   expnum),
            ('ccdname',  ccdname),
            ]:
        T.set(k, np.array([v]))
    # add values from PsfEx header cards
    keys = ['LOADED', 'ACCEPTED', 'CHI2', 'POLNAXIS',
            'POLNGRP', 'PSF_FWHM', 'PSF_SAMP', 'PSFNAXIS',
            'PSFAXIS1', 'PSFAXIS2', 'PSFAXIS3']
    if hdr['POLNAXIS'] == 0:
        # No polynomials.  Fake it.
        T.polgrp1 = np.array([0])
        T.polgrp2 = np.array([0])
        T.polname1 = np.array(['fake'])
        T.polname2 = np.array(['fake'])
        T.polzero1 = np.array([0.])
        T.polzero2 = np.array([0.])
        T.polscal1 = np.array([1.])
        T.polscal2 = np.array([1.])
        T.poldeg1 = np.array([0])
    else:
        keys.extend([
                'POLGRP1', 'POLNAME1', 'POLZERO1', 'POLSCAL1',
                'POLGRP2', 'POLNAME2', 'POLZERO2', 'POLSCAL2',
                'POLDEG1'])
    for k in keys:
        T.set(k.lower(), np.array([hdr[k]]))
    # In case of failures, these may be "-nan", "INF", etc.  Force to float64.
    for k in ['chi2', 'polzero1', 'polzero2', 'polscal1', 'polscal2']:
        T.set(k, T.get(k).astype(np.float64))
    return T

class LegacySplineSky(SplineSky):
    @classmethod
    def from_fits_row(cls, Ti):
        gridvals = Ti.gridvals.copy()
        # DR7 & previous don't have this...
        if 'sky_med' in Ti.get_columns():
            nswap = np.sum(gridvals == Ti.sky_med)
            if nswap:
                info('Swapping in SKY_JOHN values for', nswap, 'splinesky cells;', Ti.sky_med, '->', Ti.sky_john)
            gridvals[gridvals == Ti.sky_med] = Ti.sky_john
        sky = cls(Ti.xgrid, Ti.ygrid, gridvals, order=Ti.order)
        sky.shift(Ti.x0, Ti.y0)
        return sky

class NormalizedPixelizedPsfEx(PixelizedPsfEx):
    def __str__(self):
        return 'NormalizedPixelizedPsfEx'

    def getFourierTransform(self, px, py, radius):
        fft, (cx,cy), shape, (v,w) = super().getFourierTransform(px, py, radius)
        fft /= np.abs(fft[0][0])
        return fft, (cx,cy), shape, (v,w)

    def getImage(self, px, py):
        img = super(NormalizedPixelizedPsfEx, self).getImage(px, py)
        img /= np.sum(img)
        return img

    def constantPsfAt(self, x, y):
        pix = self.psfex.at(x, y)
        pix /= pix.sum()
        return PixelizedPSF(pix)

    def _sampleImage(self, img, dx, dy):
        xl,yl,img = super()._sampleImage(img, dx, dy)
        img /= img.sum()
        return xl,yl,img

def fix_weight_quantization(wt, weightfn, ext, slc):
    '''
    wt: weight-map array
    weightfn: filename
    ext: extension
    slc: slice
    '''
    # Use astropy.io.fits to open it, because it provides access
    # to the compressed data -- the underlying BINTABLE with the
    # ZSCALE and ZZERO keywords we need.
    from astropy.io import fits as fits_astropy
    from astropy.utils.exceptions import AstropyUserWarning
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=AstropyUserWarning)
        hdu = fits_astropy.open(weightfn, disable_image_compression=True)[ext]
    hdr = hdu.header
    table = hdu.data
    zquant = hdr.get('ZQUANTIZ','').strip()
    #print('Fpack quantization method:', zquant)
    if len(zquant) == 0:
        # Not fpacked?
        return True
    if zquant == 'SUBTRACTIVE_DITHER_2':
        # This method treats zeros specially so that they remain zero
        # after decompression, so return True to say that we have
        # fixed the weights.
        return True
    if zquant != 'SUBTRACTIVE_DITHER_1':
        # .... who knows what's going on?!
        raise ValueError('fix_weight_quantization: unknown ZQUANTIZ method "%s"' % zquant)
    tilew = hdr['ZTILE1']
    tileh = hdr['ZTILE2']
    imagew = hdr['ZNAXIS1']
    # This function can only handle non-tiled (row-by-row compressed)
    # files.  (This was just a choice for simplicity of
    # implementation; the ZSCALE and ZZERO arrays are stored one per
    # block, and handling general rectangular blocks (that get sliced
    # by "slc") would be significantly more complicated.)
    if tilew != imagew or tileh != 1:
        raise ValueError('fix_weight_quantization: file is not row-by-row compressed: tile size %i x %i.' % (tilew, tileh))
    zscale = table.field('ZSCALE')
    zzero  = table.field('ZZERO' )
    if not np.all(zzero == 0.0):
        raise ValueError('fix_weight_quantization: ZZERO is not all zero: [%.g, %.g]!' % (np.min(zzero), np.max(zzero)))
    if slc is not None:
        yslice,_ = slc
        zscale = zscale[yslice]
    H,_ = wt.shape
    if len(zscale) != H:
        raise ValueError('fix_weight_quantization: sliced zscale size does not match weight array: %i vs %i' % (len(zscale), H))
    print('Zeroing out', np.sum(wt <= zscale[:,np.newaxis]*0.5), 'weight-map pixels below quantization error (= median %.3g)' % (np.median(zscale)*0.5))
    wt[wt <= zscale[:,np.newaxis]*0.5] = 0.
    return True

def validate_version(fn, filetype, expnum, plver, plprocid,
                     data=None, ext=1, cpheader=False,
                     old_calibs_ok=False, truncated_ok=True, quiet=False):
    '''
    truncated_ok: the target *plver* or *plprocid* may be truncated, so only
    demand a match up to the length of those variables.  This can happen if, eg,
    the survey-ccds table has the PLVER or PLPROCID columns too short.
    '''
    if not os.path.exists(fn):
        if not quiet:
            info('File not found {}'.format(fn))
        return False
    # Check the data model
    if filetype == 'table':
        if data is None:
            T = fits_table(fn)
        else:
            T = data
        cols = T.get_columns()
        for key,targetval,strip in (('plver', plver, True),
                                    ('plprocid', plprocid, True),
                                    ('expnum', expnum, False)):
            if targetval is None:
                # Skip this check
                debug('Skipping check of', key, 'for', fn)
                continue
            if key not in cols:
                if old_calibs_ok:
                    warnings.warn('Validation: table {} is missing {} but old_calibs_ok=True'.format(fn, key))
                    continue
                else:
                    debug('WARNING: {} missing {}'.format(fn, key))
                    return False
            val = T.get(key)
            if strip:
                val = np.array([str(v).strip() for v in val])
            ok = np.all(val == targetval)
            if (not ok) and truncated_ok:
                N = len(targetval)
                val = np.array([v[:min(len(v),N)] for v in val])
                ok = np.all(val == targetval)
                if ok:
                    warnings.warn('Validation: {}={} validated only after truncating for {}'.format(key, targetval, fn))
            if not ok:
                if old_calibs_ok:
                    warnings.warn('Validation: {} {}!={} in {} table but old_calibs_ok=True'.format(key, val, targetval, fn))
                    continue
                else:
                    debug('WARNING: {} {}!={} in {} table'.format(key, val, targetval, fn))
                    return False
        return True
    elif filetype in ['primaryheader', 'header']:
        if data is None:
            if filetype == 'primaryheader':
                hdr = fitsio.read_header(fn)
            else:
                hdr = fitsio.FITS(fn)[ext].read_header()
        else:
            hdr = data

        cpexpnum = None
        if cpheader:
            # Special handling for EXPNUM in some cases
            if 'EXPNUM' in hdr and hdr['EXPNUM'] is not None:
                cpexpnum = hdr['EXPNUM']
            elif 'OBSID' in hdr:
                # At the beginning of the MzLS survey, eg 2016-01-24, the EXPNUM
                # cards are blank.  Fake up an expnum like 160125082555
                # (yymmddhhmmss), same as the CP filename.
                # OBSID   = 'kp4m.20160125T082555' / Observation ID
                # MzLS:
                obsid = hdr['OBSID']
                if obsid.startswith('kp4m.'):
                    obsid = obsid.strip().split('.')[1]
                    obsid = obsid.replace('T', '')
                    obsid = int(obsid[2:], 10)
                    cpexpnum = obsid
                    if not quiet:
                        debug('Faked up EXPNUM', cpexpnum)
                elif obsid.startswith('ksb'):
                    import re
                    # DTACQNAM are like /descache/bass/20160504/d7513.0033.fits
                    base= (os.path.basename(hdr['DTACQNAM'])
                           .replace('.fits','')
                           .replace('.fz',''))
                    cpexpnum = int(re.sub(r'([a-z]+|\.+)','',base), 10)
                    if not quiet:
                        debug('Faked up EXPNUM', cpexpnum)
            else:
                if not quiet:
                    info('Missing EXPNUM and OBSID in header')

        for key,spval,targetval,strip in (('PLVER', None, plver, True),
                                          ('PLPROCID', None, plprocid, True),
                                          ('EXPNUM', cpexpnum, expnum, False)):
            if spval is not None:
                val = spval
            else:
                if key not in hdr:
                    if old_calibs_ok:
                        warnings.warn('Validation: {} header missing {} but old_calibs_ok=True'.format(fn, key))
                        continue
                    else:
                        debug('WARNING: {} header missing {}'.format(fn, key))
                        return False
                val = hdr[key]

            if strip:
                # PLPROCID can get parsed as an int by fitsio, ugh
                val = str(val)
                val = val.strip()
            if val != targetval and truncated_ok:
                origval = val
                val = val[:len(targetval)]
                if val == targetval:
                    warnings.warn('Validation: {} validated only after truncating {} to {} for {}'.format(key, origval, val, fn))
            if val != targetval:
                if old_calibs_ok:
                    warnings.warn('Validation: {} {}!={} in {} header but old_calibs_ok=True'.format(key, val, targetval, fn))
                    continue
                else:
                    debug('WARNING: {} {}!={} in {} header'.format(key, val, targetval, fn))
                    return False
        return True

    else:
        raise ValueError('incorrect filetype')
