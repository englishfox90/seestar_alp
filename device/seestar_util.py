import re
import geocoder
from datetime import datetime, timezone
from astropy.coordinates import FK5, SkyCoord, AltAz
from astropy.time import Time
import astropy.units as u
import math
import numpy as np


class Util:
    @staticmethod
    def get_current_gps_coordinates():
        g = geocoder.ip(
            "me"
        )  # this function is used to find the current information using our IP Add
        if g.latlng is not None:  # g.latlng tells if the coordiates are found or not
            return g.latlng
        else:
            return None

    # in_ra = '17h21m29.17s'  # Center RA in hour-degree format
    # in_dec = '+80d33m44.5s'  # Center Dec in degree-minute format
    # returning a SkyCoord object in JNow coordinate system
    #    def j2000_to_jnow(in_ra, in_dec):
    #        _in_j2000 = SkyCoord(ra=in_ra, dec=in_dec, unit=(u.hourangle, u.deg), frame='icrs')
    #        _fk5 = FK5(equinox=Time(datetime.now(datetime.utc).jd, format="jd", scale="utc"))
    #        return _in_j2000.transform_to(_fk5)

    @staticmethod
    def parse_coordinate(is_j2000, in_ra, in_dec):
        _fk5 = FK5(
            equinox=Time(
                Time(datetime.now(timezone.utc), scale="utc").jd,
                format="jd",
                scale="utc",
            )
        )
        if is_j2000:
            coord_frame = "icrs"
        else:
            coord_frame = _fk5
        if isinstance(in_ra, str):
            result = SkyCoord(
                ra=in_ra, dec=in_dec, unit=(u.hourangle, u.deg), frame=coord_frame
            )
        else:
            result = SkyCoord(ra=in_ra * u.hour, dec=in_dec * u.deg, frame=coord_frame)
        if is_j2000:
            result = result.transform_to(_fk5)
        return result

    # Each model's telescope field in arcminutes, short axis then long, as in
    # front/public/seestar_models.js. Panels step the short axis in RA and the long
    # axis in Dec, the way the sky views draw the frame.
    MODEL_FOV_ARCMIN = {
        "S50": (43.8, 77.4),
        "S50 Pro": (82.8, 147.2),
        "S30": (73.2, 130.2),
        "S30 Pro": (134.6, 239.2),
    }

    # The wide angle camera's field, (RA, Dec) arcminutes on the same axes, where it
    # has been measured: the S30 Pro's from the plate solve of a wide stack.
    MODEL_WIDE_FOV_ARCMIN = {"S30 Pro": (1866.0, 3159.0)}

    @staticmethod
    def _model_key(product_model):
        compact = re.sub(r"[\s_-]+", "", str(product_model or "")).lower()
        m = re.search(r"s(30|50)(pro|p(?!lus))?", compact)
        return None if not m else f"S{m.group(1)}" + (" Pro" if m.group(2) else "")

    @staticmethod
    def model_wide_fov_deg(product_model):
        """(RA, Dec) extent in degrees of the wide angle camera's field, or None where
        it hasn't been measured."""
        field = Util.MODEL_WIDE_FOV_ARCMIN.get(Util._model_key(product_model))
        return (field[0] / 60.0, field[1] / 60.0) if field else None

    @staticmethod
    def model_fov_deg(product_model):
        """(RA, Dec) extent in degrees of a model's telescope field, the S50's when the
        model is unknown. Accepts the firmware's names: "Seestar S30P", "S30 Pro", ..."""
        key = Util._model_key(product_model) or "S50"
        short, long_ = Util.MODEL_FOV_ARCMIN[key]
        return short / 60.0, long_ / 60.0

    # take into account ra spacing factor changes depends on dec position as 1/cos(dec)
    @staticmethod
    def mosaic_next_center_spacing(in_ra, in_dec, overlap_percent, fov_deg=None):
        """[RA step in hours, Dec step in degrees] between panel centres.

        fov_deg is the (RA, Dec) field extent in degrees, from model_fov_deg; None
        keeps the S50's 0.75 x 1.29 deg this always used.
        """
        ra_deg, dec_length = fov_deg if fov_deg else (0.75, 1.29)
        ra_length = ra_deg / 15.0  # hours at dec 0, growing as 1/cos(dec)

        delta_Dec = dec_length * (100.0 - overlap_percent) / 100.0
        delta_RA = ra_length * (100.0 - overlap_percent) / 100.0

        # in case we are too close to the poles
        if abs(in_dec) > 85.0:
            return [1.0, delta_Dec]

        factor_RA = math.cos(in_dec * math.pi / 180.0)
        delta_RA /= factor_RA
        return [delta_RA, delta_Dec]

    # trim out the seconds to 1 decimal precision
    @staticmethod
    def trim_seconds(test_str):
        out = test_str
        if isinstance(test_str, str) and test_str.endswith("s"):
            index = test_str.find("m")
            if index > 0:
                str_seconds = test_str[index + 1 : len(test_str) - 1]
                float_seconds = float(str_seconds)
                # bprint(str_seconds)
                # print(float_seconds)
                out = "{}{:.1f}s".format(test_str[: index + 1], float_seconds)
        return out

    @staticmethod
    def get_JNow(ra: float, dec: float) -> SkyCoord:
        coord = SkyCoord(ra=ra, dec=dec, frame="fk5", unit="deg", equinox="JNow")
        return coord

    @staticmethod
    def get_altaz(util, ra: float, dec: float, coord_frame: AltAz) -> SkyCoord:
        coord = util.get_JNow(ra, dec)
        return coord.transform_to(coord_frame)

    @staticmethod
    def get_altaz_deg(util, ra: float, dec: float, coord_frame: AltAz) -> np.ndarray:
        coord = util.get_altaz(ra, dec, coord_frame)
        return np.asarray([coord.alt.deg, coord.az.deg])

    @staticmethod
    def get_altaz_frame(util, site_ra: float, site_dec: float) -> AltAz:
        site = util.get_JNow(site_ra, site_dec)
        altaz_frame = AltAz(location=site)
        return altaz_frame
