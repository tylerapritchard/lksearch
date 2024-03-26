from astroquery.mast import Observations
import pandas as pd
from typing import Union, Optional
import re
import logging
import warnings
from lightkurve.utils import  (
    LightkurveDeprecationWarning,
    LightkurveError,
    LightkurveWarning,
    suppress_stdout,
)

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.table import Table, join
from astropy.time import Time
from lightkurve.io import read

from .cache import cache
#from . import PACKAGEDIR, conf, config
# TODO: not sure why the above relative import didn't work. This is a temporary hack. 

import os
PACKAGEDIR = os.getcwd()
PREFER_CLOUD = True # Set from config file
DOWNLOAD_CLOUD = True # For MAST???

from memoization import cached

#from src.newlk_search.cache import cache

log = logging.getLogger(__name__)


class SearchError(Exception):
    pass

class MASTSearch(object):
    # Shared functions that are used for searches by any mission
    #    "mission",
    # Start time?
    #distance
    _REPR_COLUMNS = ["target_name","pipeline", "mission","exptime", "distance", "year","description"]

    #why is this needed here?  recursion error otherwise
    table = None

    def __init__(self, 
                 target: Optional[Union[str, tuple[float], SkyCoord]] = None, 
                 obs_table:Optional[pd.DataFrame] = None, 
                 prod_table:Optional[pd.DataFrame] = None,
                 table:Optional[pd.DataFrame] = None,
                 search_radius:Optional[Union[float,u.Quantity]] = None,
                 exptime:Optional[Union[str, int, tuple]] = (0,9999),#None,
                 mission: Optional[Union[str, list[str]]] = ["Kepler", "K2", "TESS"],
                 pipeline:  Optional[Union[str, list[str]]] = ["Kepler", "K2", "SPOC"],
                 sequence: Optional[int] = None,
                 ):
        
        self.search_radius = search_radius
        self.search_exptime = exptime
        self.search_mission = mission
        self.search_pipeline = pipeline
        self.search_sequence = sequence
        #Legacy functionality - no longer query kic/tic by integer value only
        if isinstance(target, int):
            raise TypeError("Target must be a target name string, (ra, dec) tuple, " 
                            "or astropy coordinate object")

        # If target is not None, Parse the input
        #TODO: get rid of saving prod and obs to self
        self.target = target
        if not isinstance(target, type(None)):
            self._target_from_name()
        else:
            self._target_from_table(table, obs_table, prod_table)
        
        self.table = self._update_table(self.table)




    def _target_from_name(self):
        self._parse_input(self.target)  
        self.table = self._search(
            search_radius=self.search_radius,
            exptime=self.search_exptime,
            mission=self.search_mission,
            pipeline=self.search_pipeline,
            sequence=self.search_sequence,
            )
        mask = self._filter(exptime=self.search_exptime, 
                                             project = self.search_mission,
                                             pipeline = self.search_pipeline,
                                             ) #setting provenance_name=None will return HLSPs
                                
        self.table = self.table[mask]

    def _target_from_table(self, table, obs_table, prod_table):
        #see if we were passed a joint table
        if (isinstance(table, pd.DataFrame)):
            self.table = table

        # If we don't have a name or a joint table,
        # check to see if tables were passed
        elif(isinstance(obs_table, pd.DataFrame)):
            # If we have an obs table and no name, use it
            self.obs_table = obs_table
            if(isinstance(prod_table, type(None))):
                #get the prod table if we don't have it
                prod_table = self._search_products(self)
            self.prod_table = prod_table
            self.table = self._join_tables()
        else:
            raise(ValueError("No Target or object table supplied"))
        

    def __len__(self):
        """Returns the number of products in the SearchResult table."""
        return len(self.table)

    @property
    def ra(self):
        """Right Ascension coordinate for each data product found."""
        return self.table["s_ra"].values

    @property
    def dec(self):
        """Declination coordinate for each data product found."""
        return self.table["s_dec"].values
    

    @property
    def exptime(self):
        """Exposure times for all returned products"""
        return self.table["exptime"].values
    
    @property
    def mission(self):
        """Kepler quarter or TESS sector names for each data product found."""
        return self.table["mission"].values

    @property
    def year(self):
        """Year the observation was made."""
        return self.table["year"].values

    @property
    def pipeline(self):
        """Pipeline name for each data product found."""
        return self.table["pipeline"].values

    @property
    def target_name(self):
        """Target name for each data product found."""
        return self.table["target_name"].values

    @property
    @cached
    def uris(self):
        uris = self.table["dataURI"].values

        if(PREFER_CLOUD):
            cloud_uris = self.cloud_uris
        uris = self.table["dataURI"].values
        
        mask = cloud_uris != None
        uris[mask] = cloud_uris[mask]
        return uris 

    @property
    @cached
    def cloud_uris(self):
        """ Returns the cloud uris for products in table. """
        Observations.enable_cloud_dataset()
        return np.asarray(Observations.get_cloud_uris(Table.from_pandas(self.table), full_url=True))

    @property
    def uri(self):
        "return cloud UIR & mast URI where cloud does not exist"
        raise NotImplementedError    

    #def __getattr__(self, attr):
    #    try:
    #        return getattr(self.table, attr)
    #    except AttributeError:
    #        raise AttributeError(f"'Result' object has no attribute '{attr}'")

    def __repr__(self):
        if(isinstance(self.table, pd.DataFrame)):
            return self.table[self._REPR_COLUMNS].__repr__()
        else:
            return("I am an uninitialized MASTSearch result")

    # This is a possible addition to add a hyperlink to the dataproduct homepages.
    # I think we want this anyways as this calls the pandas table html output which is nicer               
    def _repr_html_(self):
        if(isinstance(self.table, pd.DataFrame)):
            return self.table[self._REPR_COLUMNS ]._repr_html_()
        else:
            return("I am an uninitialized MASTSearch result")
    
    def __getitem__(self, key):
        if isinstance(key, (slice, int)):
            mask = np.in1d(np.arange(len(self.table)), np.arange(len(self.table))[key])
            return self._mask(mask)
        if isinstance(key, (str, list)):
            return self.table.iloc[key]
        if hasattr(key, "__iter__"):
            if len(key) == len(self.table):
                return self._mask(key)
     
    def _mask(self, mask):
        """Masks down the product and observation tables given an input mask, then rejoins them as a SearchResult."""
        indices = self.table[mask].index
        return MASTSearch(
            table = self.table.loc[indices]
            # Commented this out as we talked about not saving obs/prod table
            #obs_table=self.obs_table.loc[indices.get_level_values(0)].drop_duplicates(),
            #prod_table=self.prod_table.loc[
            #    indices.get_level_values(1)
            #].drop_duplicates(),
            
        )
    
    # may overwrite this function in the individual KEplerSearch/TESSSearch/K2Search calls?
    def _update_table(self, joint_table):
        # Ideally I'd like to replace of t_exptime and pro
        #joint_table['exptime'] = joint_table['t_exptime'].copy()
        #joint_table['pipeline'] = joint_table['provenance_name'].copy()
        #joint_table['mission'] = joint_table['obs_collection_obs'].copy()
        joint_table = joint_table.rename(columns={"t_exptime":"exptime","provenance_name":"pipeline","obs_collection_obs":"mission"})
        
        year = np.floor(Time(joint_table["t_min"], format="mjd").decimalyear)
                # # `t_min` is incorrect for Kepler products, so we extract year from the filename for those
        for idx in np.where(joint_table["pipeline"] == "Kepler")[0]:
            year[idx] = re.findall(
                r"\d+.(\d{4})\d+", joint_table["productFilename"].iloc[idx]
            )[0]
        joint_table["year"] = year.astype(int)
        joint_table["obs_collection"] = joint_table["obs_collection_prod"]


        '''
        Full list of features
        ['intentType', 'obscollection_obs', 'provennce_name',
       'instrument_name', 'project_obs', 'filters', 'wavelength_region',
       'target_name', 'target_classification', 'obs_id', 's_ra', 's_dec',
       'dataproduct_type_obs', 'proposal_pi', 'calib_level_obs', 't_min',
       't_max', 't_exptime', 'em_min', 'em_max', 'obs_title', 't_obs_release',
       'proposal_id_obs', 'proposal_type', 'sequence_number', 's_region',
       'jpegURL', 'dataURL', 'dataRights_obs', 'mtFlag', 'srcDen', 'obsid',
       'objID', 'objID1', 'distance', 'obsID', 'obs_collection_prod',
       'dataproduct_type_prod', 'description', 'type', 'dataURI',
       'productType', 'productGroupDescription', 'productSubGroupDescription',
       'productDocumentationURL', 'project_prod', 'prvversion',
       'proposal_id_prod', 'productFilename', 'size', 'parent_obsid',
       'dataRights_prod', 'calib_level_prod']'''
        

        # Other additions may include the following
        #self._add_columns("something")
        #self._add_urls_to_authors()
        #self._add_s3_url_column()      
        #self._sort_by_priority()

        
        return joint_table.reset_index()
    
    @cached
    def _search(self,
                search_radius:Union[float,u.Quantity] = None,
                exptime:Union[str, int, tuple] = (0,9999),
                cadence: Union[str, int, tuple] = None, #Kepler specific option?
                mission: Union[str, list[str]] = ["Kepler", "K2", "TESS"],
                pipeline:  Union[str, list[str]] = None,
                sequence: int = None,
                ):

        self.obs_table = self._search_obs(search_radius=search_radius, 
                                          exptime=exptime, 
                                          cadence=cadence,
                                          mission=mission,
                                          filetype=["lightcurve", "target pixel", "dv"],
                                          pipeline=pipeline,
                                          sequence=sequence,
                                          )
        self.prod_table = self._search_prod()
        joint_table = self._join_tables()


        return joint_table

        
    def _parse_input(self, search_input):
        """ Prepares target search name based on input type(Is it a skycoord, tuple, or string...)"""
        # We used to allow an int to be sent and do some educated-guess parsing
        # If passed a SkyCoord, convert it to an "ra, dec" string for MAST
        self.exact_target = False

        if isinstance(search_input, SkyCoord):
            self.target_search_string = f"{search_input.ra.deg}, {search_input.dec.deg}"
            self.SkyCoord = search_input
          
        elif isinstance(search_input, tuple):
            self.target_search_string = f"{search_input[0]}, {search_input[1]}"
            self.SkyCoord = SkyCoord(search_input, frame='icrs', unit='deg')

        elif isinstance(search_input, str):
            self.target_search_string = search_input
            self.SkyCoord = SkyCoord.from_name(search_input, frame='icrs')

            target_lower = str(search_input).lower()
            # Was a Kepler target ID passed?
            kplr_match = re.match(r"^(kplr|kic) ?(\d+)$", target_lower)
            if kplr_match:
                self.exact_target_name = f"kplr{kplr_match.group(2).zfill(9)}"
                self.exact_target = True

            # Was a K2 target ID passed?
            ktwo_match = re.match(r"^(ktwo|epic) ?(\d+)$", target_lower)
            if ktwo_match:
                self.exact_target_name = f"ktwo{ktwo_match.group(2).zfill(9)}"
                self.exact_target = True

            # Was a TESS target ID passed?
            tess_match = re.match(r"^(tess|tic) ?(\d+)$", target_lower)
            if tess_match:
                self.exact_target_name = f"{tess_match.group(2).zfill(9)}"  
                self.exact_target = True
        
        else:
           raise TypeError("Target must be a target name string or astropy coordinate object")
        


    
    def _add_s3_url_column(self, joint_table):
        # self.table would updated to have an extra column of s3 URLS if possible
        Observations.enable_cloud_dataset()
        cloud_uris = Observations.get_cloud_uris(Table.from_pandas(joint_table), full_url=True)
        joint_table["cloud_uri"] = cloud_uris
        return joint_table   
    
    @cached
    def _search_obs(self, 
        search_radius=None,
        filetype=["lightcurve", "target pixel", "dv"],
        mission=["Kepler", "K2", "TESS"],
        pipeline=None,
        exptime=(0, 9999),
        sequence=None,
        cadence = None,):
        # Helper function that returns a Search Result object containing MAST products
        # combines the results of Observations.query_criteria (called via self.query_mast) and Observations.get_product_list

        '''if [bool(quarter), 
            bool(campaign),
            bool(sector)].count(True) > 1:
                raise LightkurveError("Ambiguity Error; multiple quarter/campaign/sector specified."
                    "If searching for specific data across different missions, perform separate searches by mission.")'''
        
        # Is this what we want to do/ where we want the error thrown?
        if filetype == 'ffi':
            raise SearchError(f"FFI search not implemented in MASTSearch. Please use TESSSearch.")
        
        # if a quarter/campaign/sector is specified, search only that mission
        '''if quarter is not None:
            mission = ["Kepler"]
        if campaign is not None:
            mission = ["K2"]
        if sector is not None:
            mission = ["TESS"]  '''  
        # Ensure mission is a list
        mission = np.atleast_1d(mission).tolist()
        if pipeline is not None:
            pipeline = np.atleast_1d(pipeline).tolist()
            # If pipeline "TESS" is used, we assume it is SPOC
            pipeline = np.unique(
                [p if p.lower() != "tess" else "SPOC" for p in pipeline]
            )
            
        # Speed up by restricting the MAST query if we don't want FFI image data
        # At MAST, non-FFI Kepler pipeline products are known as "cube" products,
        # and non-FFI TESS pipeline products are listed as "timeseries"
        extra_query_criteria = {}
        filetype_query_criteria = {"lightcurve": "timeseries", "target pixel": "cube"}

        extra_query_criteria["dataproduct_type"] = [filetype_query_criteria[file.lower()]
                                                    for file in filetype
                                                    if(file.lower() in filetype_query_criteria.keys())]

        #from astroquery.mast import Observations
        observations = self._query_mast(
            search_radius=search_radius,
            project=mission,
            provenance_name=pipeline,
            exptime=exptime,
            sequence_number=sequence,
            **extra_query_criteria,
        )
        log.debug(
            f"MAST found {len(observations)} observations. "
            "Now querying MAST for the corresponding data products."
        )
        if len(observations) == 0:
            raise SearchError(f"No data found for target {self.target}.")

        return observations

    @cached
    def _query_mast(self, 
        search_radius: Union[float, u.Quantity, None] = None,
        project: Union[str, list[str]] = ["Kepler", "K2", "TESS"],
        provenance_name: Union[str, list[str], None] = None,
        exptime: Union[int, float, tuple, type(None)] = (0,9999),#None,
        sequence_number: Union[int, list[int], None] = None,
        **extra_query_criteria):

        from astroquery.exceptions import NoResultsWarning, ResolverError

        #**extra_query_criteria,):
        # Constructs the appropriate query for mast
        log.debug(f"Searching for {self.target} with {exptime} on project {project}")

        # We pass the following `query_criteria` to MAST regardless of whether
        # we search by position or target name:
        query_criteria = {"project": project, **extra_query_criteria}
        if provenance_name is not None:
            query_criteria["provenance_name"] = provenance_name
        if sequence_number is not None:
            query_criteria["sequence_number"] = sequence_number
        if exptime is not None:
            query_criteria["t_exptime"] = exptime

        if self.exact_target and (search_radius is None):
            log.debug(f"Started querying MAST for observations with exact name {self.exact_target_name}")

            #do an exact name search with target_name= 
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=NoResultsWarning)
                warnings.filterwarnings("ignore", message="t_exptime is continuous")
                obs = Observations.query_criteria(target_name = self.exact_target_name, **query_criteria)
       
            if len(obs) > 0:
                # astroquery does not report distance when querying by `target_name`;
                # we add it here so that the table returned always has this column.
                obs["distance"] = 0.0
                return obs.to_pandas()
        else:
            if search_radius is None:
                search_radius = 0.0001 * u.arcsec
                
            elif not isinstance(search_radius, u.quantity.Quantity):
                log.warning(f'Search radius {search_radius} units not specified, assuming arcsec')
                search_radius = search_radius * u.arcsec

            query_criteria["radius"] = str(search_radius.to(u.deg))

            try:
                log.debug(
                    "Started querying MAST for observations within "
                    f"{search_radius.to(u.arcsec)} arcsec of objectname='{self.target}'."
                    f"Via {self.target_search_string} search string and query_criteria: "
                    f"{query_criteria}"
                )
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=NoResultsWarning)
                    warnings.filterwarnings("ignore", message="t_exptime is continuous")
                    obs = Observations.query_criteria(objectname = self.target_search_string, **query_criteria)
                obs.sort("distance")
                return obs.to_pandas()
            except ResolverError as exc:
                # MAST failed to resolve the object name to sky coordinates
                raise SearchError(exc) from exc

        return obs.to_pandas()

    @cached
    def _search_prod(self):
        # Use the search result to get a product list
        products = Observations.get_product_list(Table.from_pandas(self.obs_table))
        return products.to_pandas()

    def _join_tables(self):

        joint_table = pd.merge(
            self.obs_table.reset_index().rename({"index": "obs_index"}, axis="columns"),
            self.prod_table.reset_index().rename(
                {"index": "prod_index"}, axis="columns"
            ),
            on="obs_id",
            how="left",
            suffixes=("_obs", "_prod"),
        ).set_index(["obs_index", "prod_index"])
        
        log.debug(f"MAST found {len(joint_table)} matching data products.")
        return joint_table
   
    @property
    def timeseries(self):
        """return a MASTSearch object with self.table only containing products that are a time-series measurement"""
        #mask = self.table.productFilename.str.endswith("lc.fits")
        # Not sure about the call below. Will exptime already have been handled in the mast search?
        mask = self._filter_product_endswith('lightcurve') 
        return(self._mask(mask))
    
    @property
    def cubedata(self):
        """ return a MASTSearch object with self.table only containing products that are image cubes """
        mask = self._filter_product_endswith('target pixel') 
        
        #return self._cubedata()
        return (self._mask(mask))
    
    
    #def _cubedata(self):
    #    """ passthrough that mission searches can call """
    #    mask = self.table.productFilename.str.endswith("tp.fits")
    #    return(self._mask(mask))


    def limit_results(self, limit: int):
        mask = np.ones(len(self.table), dtype=bool)
        mask[limit:] = False
        return (self._mask(mask)) 
    
    @property    
    def dvreports(self):
        """return a MASTSearch object with self.table only containing products that are data validation pdf files"""
        #mask = self.table.productFilename.str.endswith(".pdf")
        mask = self._filter_product_endswith(filetype='dvreport')
        return(self._mask(mask))
    
    def _sort_by_priority():
        # Basic sort
        raise NotImplementedError("To Do")
    def _add_columns():
        raise NotImplementedError("To Do")
    
    def _add_urls_for_pipeline():
        raise NotImplementedError("To Do")
    
    def _add_kepler_sequence_num(self):
        seq_num = self.table["sequence_number"].values.astype(str)

        # Kepler sequence_number values were not populated at the time of
        # writing this code, so we parse them from the description field.
        mask = ((self.table["project"] == "Kepler") &
                self.table["sequence_number"].isna())
        re_expr = r".*Q(\d+)"
        seq_num[mask] = [re.findall(re_expr, item[0])[0] if re.findall(re_expr, item[0]) else "" for item in self.table.loc[mask,["description"]].str.values]


    def _filter_product_endswith(self, 
                       filetype: str,
                       ):
        mask =  np.zeros(len(self.table), dtype=bool)
        #This is the dictionary of what files end with that correspond to each allowed file type
        ftype_suffix = {
            "lightcurve": ["lc.fits"],
            "target pixel": ["tp.fits", "targ.fits.gz"],
            "dvreport": ["dvr.pdf","dvm.pdf","dvs.pdf"]
        }

        for value in ftype_suffix[filetype]:
            mask |= self.table.productFilename.str.endswith(value)        
        return mask
    
    def _filter(self,
            exptime: Union[str, int, tuple[int], type(None)] = (0,9999),
            project: Union[str, list[str]] = ["Kepler", "K2", "TESS"],
            pipeline: Union[str, list[str]] = ["kepler", "k2", "spoc"],
            filetype: Union[str, list[str]] = ["target pixel", "lightcurve", "dvreport"], #lightcurve, target pixel, report
        ) -> pd.DataFrame:
        # Modify this so that it can choose what types of products to keep
        """Since this will be used by mission specific search we want this to filter:
            Filetype
            ExposureTime/cadence
            Pipe(Provenance)/Project - e.g. (SPOC/TESSSpoc)
            <Segment/Quarter/Sector/Campaign> this will be in mission specific search
            
            """

        self.search_exptime = exptime
        mask = np.zeros(len(self.table), dtype=bool)


        #First filter on filetype
        file_mask = mask.copy()


        #This is the list of allowed filetypes we can interact with
        allowed_ftype = ["lightcurve", "target pixel", "dvreport"]

        filter_ftype =  [file.lower() for file in filetype if file.lower() in allowed_ftype]
         #First filter on filetype
        
        if len(filter_ftype) == 0:
            filter_ftype = allowed_ftype
            log.warning("Invalid filetype filtered. Returning all data.")

        file_mask = mask.copy()
        for ftype in filter_ftype:
            file_mask |= self._filter_product_endswith(ftype)

        #Next Filter on project
        project_mask = mask.copy()
        if not isinstance(project_mask, type(None)):
            for proj in project:
                project_mask |= self.table.project_obs.values == proj
        else:
            project_mask = np.logical_not(project_mask)

        #Next Filter on pipeline (provenance_name in mast table)
        provenance_mask = mask.copy()
        if  not isinstance(pipeline, type(None)):
            for a in np.atleast_1d(pipeline).tolist():
                provenance_mask |=  self.table.provenance_name.str.lower() == a.lower()
        else:
            provenance_mask = np.logical_not(provenance_mask)
            
        # Filter by cadence
        if(not isinstance(exptime, type(None))):
            exptime_mask = self._mask_by_exptime(exptime)
        else:
            exptime_mask = not mask

        '''# If no products are left, return an empty dataframe with the same columns
        if sum(mask) == 0:
            return pd.DataFrame(columns = products.keys())

        products = products[mask]

        products.sort_values(by=["distance", "productFilename"], ignore_index=True)

        return products'''
        # I think this hidden filter function should now just return the mask
        mask = file_mask & project_mask & provenance_mask & exptime_mask
        return mask
    

    # Again, may want to add to self.mask if we go that route. 
    def _mask_by_exptime(self, exptime):
        """Helper function to filter by exposure time.
        Returns a boolean array """
        if isinstance(exptime, (int, float)):
            mask = self.table.t_exptime == exptime
        elif isinstance(exptime, tuple):
            mask = (self.table.t_exptime >= min(exptime) & (self.table.t_exptime <= max(exptime)))
        elif isinstance(exptime, str):
            exptime = exptime.lower()
            if exptime in ["fast"]:
                mask = self.table.t_exptime < 60
            elif exptime in ["short"]:
                mask = (self.table.t_exptime >= 60) & (self.table.t_exptime <= 120)
            elif exptime in ["long", "ffi"]:
                mask = self.table.t_exptime > 120
            elif exptime in ["shortest"]:
                mask = self.table.t_exptime == min(self.table.t_exptime)
            elif exptime in ["longest"]:
                mask = self.table.t_exptime == max(self.table.t_exptime)
            else:
                mask = np.ones(len(self.table.t_exptime), dtype=bool)
                log.debug('invalid string input. No exptime filter applied')
        return mask

    #@cache
    def _download_one(self, row,
                 cloud: bool = True,
                 cloud_only: bool = False, 
                 cache: bool = True,
                 download_dir: str = '.'):
        manifest = Observations.download_products(Table().from_pandas(row.to_frame(name=" ").transpose()),
                                                  download_dir = download_dir,
                                                  cache = cache, 
                                                  cloud_only = cloud_only)
        return manifest[0]
    
    def Download(self,
                 cloud: bool = True,
                 cloud_only: bool = False, 
                 cache: bool = True,
                 download_dir: str =  "~/."):
        #TODO magic caching
        """ 
            Should this download to the local directory by default or to a hidden cache directory?
            If local - may be more convenient in a world without lightkurve
            Cachine more seamless if a user is searching for the same file(s) accross different project
            directories 
        """
        if(cloud):
            Observations.enable_cloud_dataset()

        #manifest = Observations.download_products(Table.from_pandas(self.table),
        #                                          download_dir = download_dir,
        #                                          cache = cache, 
        #                                          cloud_only = cloud_only)
        manifest = [self._download_one(row, 
                                       cloud_only,
                                       cache,
                                       download_dir) for _,row in self.table.iterrows()]
        return manifest

    

    def _default_download_dir(self):
        # TODO: again, I can't inport config so hard code it for now
        # return config.get_cache_dir()
        return "~/."

    


    



class TESSSearch(MASTSearch):

    def __init__(self, 
        target: [Union[str, tuple[float], SkyCoord]],   
        obs_table:Optional[pd.DataFrame] = None, 
        prod_table:Optional[pd.DataFrame] = None,
        table:Optional[pd.DataFrame] = None,
        search_radius:Optional[Union[float,u.Quantity]] = None,
        exptime:Optional[Union[str, int, tuple]] = (0,9999),
        pipeline:  Optional[Union[str, list[str]]] = None,
        sector: Optional[int] = None,
        ):
        
        super().__init__(target=target, 
                         mission=["TESS"], 
                         obs_table=obs_table, 
                         prod_table=prod_table, 
                         table=table, 
                         search_radius=search_radius, 
                         exptime=exptime, 
                         pipeline=pipeline, 
                         sequence=sector)
        # Uncommenting this ffi search got rid of the non-ffi search results. Debug later...
        #self._add_ffi_products()
    
    def _add_ffi_products(self):
        #get the ffi info for the targets
        ffi_info = self._get_ffi_info()
        #add the ffi info to the table
        self.table = pd.concat([self.table, ffi_info])
        #sort the table



    # FFIs only available when using TESSSearch. 
    # Use TESS WCS to just return a table of sectors and dates? 
    # Then download_ffi requires a sector and time range?
        
    def _get_ffi_info(self):
        from tesswcs import pointings
        from tesswcs import WCS


        log.debug("Checking tesswcs for TESSCut cutouts")
        tesscut_desc=[]
        tesscut_mission=[]
        tesscut_tmin=[]
        tesscut_tmax=[]
        tesscut_exptime=[]
        tesscut_seqnum=[]

        # Check each sector / camera / ccd for observability
        #Submit a tesswcs PR for to convert table to pandas

        for i,row in pointings.to_pandas().iterrows():
            tess_ra = row['RA']
            tess_dec = row['Dec']
            tess_roll = row['Roll']
            sector = row['Sector'].astype(int)

            AddSector = False
            sector_camera = []
            sector_ccd = []
            for camera in np.arange(1, 5):
                for ccd in np.arange(1, 5):
                    # predict the WCS
                    wcs = WCS.predict(tess_ra, tess_dec, tess_roll , camera=camera, ccd=ccd)
                    # check if the target falls inside the CCD
                    if wcs.footprint_contains(self.SkyCoord):
                        AddSector = True
                        sector_camera = sector_camera.append(camera)
                        sector_ccd = sector_ccd.append(camera)

            if AddSector:
                log.debug(f"Target Observable in Sector {sector}, Camera {sector_camera}, CCD {sector_ccd}")
                tesscut_desc.append(f"TESS FFI Cutout (sector {sector})")
                tesscut_mission.append(f"TESS Sector {sector:02d}")
                tesscut_tmin.append(row['Start']- 2400000.5) # Time(row[5], format="jd").iso)
                tesscut_tmax.append(row['End']- 2400000.5) # Time(row[6], format="jd").iso)
                tesscut_exptime.append(self._sector2ffiexptime(sector))
                tesscut_seqnum.append(sector)

        
        # Build the ffi dataframe from the observability
        n_results = len(tesscut_seqnum)
        ffi_result = pd.DataFrame({"description" : tesscut_desc,
                                          "mission": tesscut_mission,
                                          "target_name" : [self.target_search_string] * n_results,
                                          "targetid" : [self.target_search_string] * n_results,
                                          "t_min" : tesscut_tmin,
                                          "t_max" : tesscut_tmax,
                                          "exptime" : tesscut_exptime,
                                          "productFilename" : ["TESScut"] * n_results,
                                          "provenance_name" : ["TESScut"] * n_results,
                                          "pipeline" : ["TESScut"] * n_results,
                                          "distance" : [0] * n_results,
                                          "sequence_number" : tesscut_seqnum,
                                          "project" : ["TESS"] * n_results,
                                          "obs_collection" : ["TESS"] * n_results})
        
        if len(ffi_result) > 0:
            log.debug(f"Found {n_results} matching cutouts.")
        else:
            log.debug("Found no matching cutouts.")

        return ffi_result
    
    def _sector2ffiexptime(self, sector):
        if sector <27:
            return 1800
        elif ((sector >= 27) &
              (sector <= 55)):
            return 600
        elif sector >= 56:
            return 200
        

    def sort_TESS():
        # base sort + TESS HLSP handling?
        raise NotImplementedError

    def download_ffi():
        raise NotImplementedError

    
    # This was in Christina's PR to search. Is this a way we want to handle HLSPs?
    #def _mask_bad_authors(authors):
    # """Returns a mask to remove authors we don't have readers for."""
    # bad_authors = np.asarray([author not in AUTHOR_LINKS.keys() for author in authors])
    # if bad_authors.any():
    #     log.warn(
    #         f"Authors {np.unique(authors[bad_authors])} have been removed as `lightkurve` does not have a specific reader for these HLSPs.",
    # )
    # return ~bad_authors
























    
class KeplerSearch(MASTSearch):
    
    def __init__(self,  
        target: [Union[str, tuple[float], SkyCoord]],   
        obs_table:Optional[pd.DataFrame] = None, 
        prod_table:Optional[pd.DataFrame] = None,
        table:Optional[pd.DataFrame] = None,
        search_radius:Optional[Union[float,u.Quantity]] = None,
        exptime:Optional[Union[str, int, tuple]] = (0,9999),
        pipeline:  Optional[Union[str, list[str]]] = None,
        quarter: Optional[int] = None,
        month: Optional[int] = None):
        
        super().__init__(target=target, 
                         mission=["Kepler"], 
                         obs_table=obs_table, 
                         prod_table=prod_table, 
                         table=table, 
                         search_radius=search_radius, 
                         exptime=exptime, 
                         pipeline=pipeline, 
                         sequence=None)
        self._add_kepler_mission_product()
        # Can't search mast with quarter/month directly, so filter on that after the fact. 
        self.table = self.table[self._filter_kepler(quarter, month)]

    def _fix_times():
        # Fixes Kepler times
        raise NotImplementedError

    def _handle_kbonus(self):
        # KBONUS times are masked as they are invalid for the quarter data
        #kbonus_mask = self.table["pipeline"] == "KBONUS-BKG"
        raise NotImplementedError

    def _add_kepler_mission_product(self):
        # Some products are HLSPs and some are mission products
        mission_product = np.zeros(len(self.table), dtype=bool)
        mission_product[self.table["pipeline"] == "Kepler"] = True
        self.table['mission_product'] = mission_product

    @property
    def HLSPs(self):
        """return a MASTSearch object with self.table only containing High Level Science Products"""
        mask = self.table['mission_product'] 
        return(self._mask(~mask))
    
    @property
    def mission_products(self):
        """return a MASTSearch object with self.table only containing Mission Products"""
        mask = self.table['mission_product'] 
        return(self._mask(mask))

    def get_sequence_number(self):
        # Kepler sequence_number values were not populated at the time of
        # writing this code, so we parse them from the description field.

        seq_num = self.table["sequence_number"].values.astype(str)

        mask = ((self.table["project"] == "Kepler") &
                self.table["sequence_number"].isna())
        re_expr = r".*Q(\d+)"
        seq_num[mask] = [re.findall(re_expr, item[0])[0] if re.findall(re_expr, item[0]) else "" for item in joint_table.loc[mask,["description"]].values]

    def _filter_kepler(
            self, 
            quarter: Union[int, list[int]] = None,
            month: Union[int, list[int]]= None,
        ) -> pd.DataFrame:
        import os
        # Filter Kepler product by month/quarter
        # TODO: should this return the mask or replace self.table directly? I'm leaning toward replace directly
        products = self.table

        mask = np.ones(len(products), dtype=bool)

        if sum(mask) == 0:
            return products

        # Identify quarter by the description.
        # This is necessary because the `sequence_number` field was not populated
        # for Kepler prime data at the time of writing this function.
        if quarter is not None:
            quarter_mask = np.zeros(len(products), dtype=bool)
            for q in np.atleast_1d(quarter).tolist():
                quarter_mask += products['description'].str.endswith(f"Q{q}")
            mask &= quarter_mask

        # For Kepler short cadence data the month can be specified
        if month is not None:
            month = np.atleast_1d(month).tolist()
            # Get the short cadence date lookup table.
            table = pd.read_csv(
                os.path.join(PACKAGEDIR, "data", "short_cadence_month_lookup.csv")
            )
            # The following line is needed for systems where the default integer type
            # is int32 (e.g. Windows/Appveyor), the column will then be interpreted
            # as string which makes the test fail.
            table["StartTime"] = table["StartTime"].astype(str)
            # Grab the dates of each of the short cadence files.
            # Make sure every entry has the correct month
            is_shortcadence = mask & products['description'].str.contains("Short")

            for idx in np.where(is_shortcadence)[0]:
                quarter = np.atleast_1d(int(products["description"][idx].split(" - ")[-1].replace("-", "")[1:])).tolist()
                date = products['dataURI'][idx].split("/")[-1].split("-")[1].split("_")[0]
                # Check if the observation date matches the specified Quarter/month from the lookup table
                if date not in table["StartTime"][table['Month'].isin(month) & table['Quarter'].isin(quarter)].values:
                    mask[idx] = False
        return mask
    


    def sortKepler():
        sort_priority = {"Kepler": 1, 
                         "KBONUS-BKG": 2, 
                         }
        df = self.table
        df["sort_order"] = self.table['pipeline'].map(sort_priority).fillna(9)
        df.sort_values(by=["distance", "project", "sort_order", "start_time", "exptime"], ignore_index=True, inplace=True)
        self.table = df




class K2Search(MASTSearch):
    def __init__(self,
        target: [Union[str, tuple[float], SkyCoord]],   
        obs_table:Optional[pd.DataFrame] = None, 
        prod_table:Optional[pd.DataFrame] = None,
        table:Optional[pd.DataFrame] = None,
        search_radius:Optional[Union[float,u.Quantity]] = None,
        exptime:Optional[Union[str, int, tuple]] = (0,9999),
        pipeline:  Optional[Union[str, list[str]]] = None,
        campaign: Optional[int] = None,
        ):
        
        super().__init__(target=target, 
                         mission=["K2"], 
                         obs_table=obs_table, 
                         prod_table=prod_table, 
                         table=table, 
                         search_radius=search_radius, 
                         exptime=exptime, 
                         pipeline=pipeline, 
                         sequence=campaign)
        self._add_K2_mission_product()
        # Can't search mast with quarter/month directly, so filter on that after the fact. 

    def _add_K2_mission_product(self):
        # Some products are HLSPs and some are mission products
        mission_product = np.zeros(len(self.table), dtype=bool)
        mission_product[self.table["pipeline"] == "K2"] = True
        self.table['mission_product'] = mission_product

    #@properties like campaigns (seasons?)
    def _fix_K2_sequence(self):
        # K2 campaigns 9, 10, and 11 were split into two sections, which are
        # listed separately in the table with suffixes "a" and "b"
        
        
        #if obs_project == "K2" and result["sequence_number"][idx] in [9, 10, 11]:
        #    for half, letter in zip([1, 2], ["a", "b"]):
        #        if f"c{tmp_seqno}{half}" in result["productFilename"][idx]:
        #            obs_seqno = f"{int(tmp_seqno):02d}{letter}"
        raise NotImplementedError

    '''def search_cubedata(hlsp=False):
        # Regular _search_cubedata + processing
        raise NotImplementedError'''

    def parse_split_campaigns():
        raise NotImplementedError
    
    def sortK2():
        raise NotImplementedError


# Potential HLSP reader class in a different .py file?
