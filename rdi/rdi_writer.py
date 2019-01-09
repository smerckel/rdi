from collections import defaultdict
from itertools import chain
import datetime
import glob
import os
import sys

import numpy as np
from netCDF4 import Dataset

import ndf
from rdi import __VERSION__
from rdi.rdi_reader import get_ensemble_time, unixtime_to_RTC

from rdi.coroutine import coroutine, Coroutine

TransformationTranslations = dict(Earth = 'east north up error'.split(),
                                  Ship = 'starboard forward up error'.split(),
                                  Instrument = 'x y z error'.split(),
                                  Beam = 'beam1 beam2 beam3 beam4'.split())

PARAMETERTRANSLATIONS = dict(XdcrDepth='Depth', Salin='Salinity', Temp='Temperature', Timestamp='Time')

# all know and decoded parameters:
PARAMETERS = dict(fixed_leader = 'CPU_ver CPU_rev Sys_Freq Beam_Pattern Sensor_Cfg Xdcr_Head Xdcr_Facing Beam_Angle Beam_Cfg Real_Data N_Beams N_Cells N_PingsPerEns DepthCellSize Blank WaterMode CorrThresshold Code_Repts MinPG ErrVelThreshold TimeBetweenPings RawCoordXrfm CoordXfrm CoordXfrmOptions Vel_field1 Vel_field2 Vel_field3 Vel_field4 EA EB Sensors Sensors_Avail FirstBin XmtLength WL_Start WL_End FalseTargetThreshold LagDistance CPUBoardSerial Bandwidth XmtPower SystemSerialNumber'.split(),
                  variable_leader = 'Ensnum RTC BitResult Soundspeed XdcrDepth Heading Pitch Roll Salin Temp MPT Hdg_SD Pitch_SD Roll_SD ADC ErrorStatus Press PressVar RTCY2K'.split(),
                  velocity = 'Velocity1 Velocity2 Velocity3 Velocity4'.split(),
                  correlation = 'Corr1 Corr2 Corr3 Corr4 Corr_AVG'.split(),
                  echo = 'Echo1 Echo2 Echo3 Echo4 Echo_AVG'.split(),
                  percent_good = 'PG1 PG2 PG3 PG4'.split(),
                  bottom_track = 'PPE Delay CorrMin AmpMin PGMin Mode ErrVelMax Range1 Range2 Range3 Range4 BTVel1 BTVel2 BTVel3 BTVel4 Corr1 Corr2 Corr3 Corr4 Amp1 Amp2 Amp3 Amp4 PG1 PG2 PG3 PG4 ReflMin ReflNear ReflFar ReflVel1 ReflVel2 ReflVel3 ReflVel4 ReflCorr1 ReflCorr2 ReflCorr3 ReflCorr4 ReflInt1 ReflInt2 ReflInt3 ReflInt4 ReflPG1 ReflPG2 ReflPG3 ReflPG4 BTdepthMax RSSI1 RSSI2 RSSI3 RSSI4 Gain'.split())

# Parameters that may get added during processing (not present in RDI format):
PARAMETERS['variable_leader']+=['Timestamp']
PARAMETERS['fixed_leader']+=['OriginalCoordXfrm']


DEFAULT_PARAMETERS = dict(velocity = PARAMETERS['velocity'],
                          correlation = PARAMETERS['correlation'],
                          percent_good = PARAMETERS['percent_good'],
                          variable_leader = 'Timestamp Ensnum Soundspeed XdcrDepth Heading Pitch Roll Salin Temp'.split(),
                          bottom_track = 'BTVel1 BTVel2 BTVel3 BTVel4 PG1 PG2 PG3 PG4 Range1 Range2 Range3 Range4'.split(),  
                          fixed_leader = 'Sys_Freq Xdcr_Facing N_Beams N_Cells N_PingsPerEns DepthCellSize Blank CoordXfrm WaterMode FirstBin SystemSerialNumber OriginalCoordXfrm'.split(),
                          )

def rad(x):
    return x*np.pi/180.


class Writer(Coroutine):
    YEAR = 2000
    
    def __init__(self):
        super().__init__()
        self.is_context_manager = False
        self.__set_parameter_list()
        
    @coroutine
    def coro_write_ensembles(self,fd):
        config = None
        scalar_data = defaultdict(lambda : [])
        vector_data = defaultdict(lambda : [])
        while True:
            try:
                ensemble = (yield)
                config = self.__write_ensemble(ensemble, fd, config, scalar_data, vector_data)
            except GeneratorExit:
                break

    def __enter__(self):
        self.is_context_manager = True
        try:
            self.enter()
        except AttributeError:
            pass    
        
    def __exit__(self, type, value, tb):
        self.is_context_manager = True
        try:
            self.exit(type, value, tb)
        except AttributeError:
            pass    
        
    def set_custom_parameter(self, section, *name, dtype=None):
        ''' Mark a non-standard parameter as one that should be written to file.

        Parameters
        ----------
        section : string
                  name of the section the parameter lives in (key of ensemble dictionary)
        *name   : variable list of arguments of parameters within this section
        dtype   : string
                  specifies the data type (scalar or vector)
        '''
        if dtype is None:
            raise ValueError('dtype is not set explicitly.')
        for _name in name:
            self.custom_parameters[dtype].append((section, _name))

    def clear_parameter_list(self, dtype=None):
        ''' Clear a parameter list
        
        Parameters
        ----------
        dtype : string
                datatype (config, scalar or vector)

        Clears all parameters, or only scalar or vectors
        '''
        if dtype is None:
            for k in list(self.parameters.keys()):
                self.parameters[k].clear()
        else:
            self.parameters[dtype].clear()

    def add_parameter_list(self, section, *p):
        ''' Add parameters from a section to the parameter list
        
        Parameters
        ----------
        section : string
                  name of section. The section "fixed_leader" has datatype "config", 
                  the sections "variable_leader" and "bottom_track" are of dataype scalar, 
                  and all others are vectors.
        '''
        if section=='fixed_leader':
            dtype='config'
        elif section=='variable_leader' or section=='bottom_track':
            dtype='scalar'
        else:
            dtype='vector'
        for k in p:
            self.parameters[dtype].append((section,k))
            
    def read_scalar_data(self, data, ens):
        ''' Reads scalar data from ensemble ens and stores it in data

        Parameters
        ----------
        data : dictionary
               data dictionary
        ens  : dictionary
               ensemble dictionary
        '''
        for s, k in self.parameters['scalar']:
            # single out special cases
            if s=='variable_leader' and k=='Timestamp':
                try:
                    tm = ens['variable_leader']['Timestamp']
                except KeyError:
                    tm = get_ensemble_time(ens)
                data['Time'].append(tm)
                continue
            kt = self.__get_keyname(s, k)
            if s=='variable_leader' and k in ['Roll', 'Pitch', 'Heading']:
                # apply conversion to radians.
                data[kt].append(rad(ens[s][k]))
            else:
                data[kt].append(ens[s][k])
        # add any customized parameters.
        for s, p in self.custom_parameters['scalar']:
            key = "%s %s"%(s,p)
            data[key].append(ens[s][p])
    
    def read_vector_data(self, data, ens):
        ''' Reads vector data from ensemble ens and stores it in data

        Parameters
        ----------
        data : dictionary
               data dictionary
        ens  : dictionary
               ensemble dictionary
        '''

        for s, k in self.parameters['vector']:
            kt = self.__get_keyname(s, k)
            data[kt].append(ens[s][k])
        # add any customized parameters.
        for s, p in self.custom_parameters['vector']:
            if s not in ens.keys():
                continue
            if p=="*":
                for k, v in ens[s].items():
                    try:
                        data[k].append(v)
                    except KeyError:
                        pass
            else:
                try:
                    data[p].append(ens[s][p])
                except KeyError:
                    pass

    def is_masked_array(self,v):
        ''' Checks whether v is a masked array
        
        Parameters
        ----------
        v : array-like

        Returns
        -------
        ma : boolean
             True is v is masked_array. False otherwise
        '''
        g = (_v for _v in v)
        ma = False
        for _v in g:
            if isinstance(_v, np.ma.core.MaskedArray):
                ma = True
                break
        return ma
            
    def array2d_from_list(self,v):
        ''' return list v as an array or masked_array, depening on wheterh v is masked or not '''
        if self.is_masked_array(v):
            return np.ma.vstack(v)
        else:
            return np.vstack(v)

    def array1d_from_list(self,v):
        ''' return list v as an array or masked_array, depening on whether v has any nan's '''
        condition = np.isnan(v)
        if np.any(condition):
            return np.ma.masked_array(v, condition)
        else:
            return np.array(v)
        
    # subclass this class and implement these methods below.
    def write_configuration(self, config, fd):
        raise NotImplementedError("This method is not implemented. Subclass this class...")

    def write_header(self, config, fd):
        raise NotImplementedError("This method is not implemented. Subclass this class...")

    def write_array(self,config, scalar_data, vector_data, fd):
        raise NotImplementedError("This method is not implemented. Subclass this class...")

    # Private methods
    def __write_ensemble(self, ensemble, fd, config, scalar_data, vector_data):
        # method that does the actual writing
        if not config:
            config = ensemble['fixed_leader']
            self.write_configuration(config, fd)
            self.write_header(config, fd)
        self.read_scalar_data(scalar_data,ensemble)
        self.read_vector_data(vector_data, ensemble)
        self.write_array(config, scalar_data, vector_data, fd)
        scalar_data.clear()
        vector_data.clear()
        return config
    

    def __set_parameter_list(self):
        # sets the default parameters lists from DEFAULT PARAMETERS
        self.parameters = dict(config=[], scalar=[], vector=[])
        for k in DEFAULT_PARAMETERS['fixed_leader']:
            self.parameters['config'].append(('fixed_leader',k))
        for s in ['variable_leader', 'bottom_track']:
            for k in DEFAULT_PARAMETERS[s]:
                self.parameters['scalar'].append((s,k))
        for s in ['velocity', 'correlation', 'percent_good']:
            for k in DEFAULT_PARAMETERS[s]:
                self.parameters['vector'].append((s,k))
        self.custom_parameters = dict(config=[], scalar=[], vector=[])

            
    def __get_keyname(self, s, k):
        try:
            kt = PARAMETERTRANSLATIONS[k]
        except KeyError:
            kt = k
        if s == 'variable_leader':
            m = kt
        else:
            m = "%s %s"%(s, kt)
        return m
    

# NetCDF format specifiers:
# just for reference...        

# 'f4' (32-bit floating point),
# 'f8' (64-bit floating point),
# 'i4' (32-bit signed integer),
# 'i2' (16-bit signed integer),
# 'i8' (64-bit signed integer),
# 'i1' (8-bit signed integer),
# 'u1' (8-bit unsigned integer),
# 'u2' (16-bit unsigned integer),
# 'u4' (32-bit unsigned integer),
# 'u8' (64-bit unsigned integer),
# 'S1' (single-character string). 



class NetCDFWriter(Writer):
    VARIABLES = dict(N_Cells = ('u1', 'scalar', '-'), 
                     N_PingsPerENS = ('u1', 'scalar', '-'),
                     Blank = ('f4', 'scalar', 'm'),
                     FirstBin = ('f4', 'scalar', 'm'),
                     #
                     Roll = ('f4', 'onedim', 'deg'), 
                     Pitch = ('f4', 'onedim', 'deg'), 
                     Heading = ('f4', 'onedim','deg'), 
                     Soundspeed = ('f4', 'onedim', 'm/s'), 
                     Salin = ('f4', 'onedim', 'SA'), 
                     Temp = ('f4', 'onedim', 'Celcius'), 
                     Press = ('f4', 'onedim', 'dbar?'), 
                     Ensnum = ('i8', 'onedim', '-'), 
                     Time = ('f8', 'onedim', 's'),
                     #
                     Velocity1 = ('f4', 'twodim', 'm/s'), 
                     Velocity2 = ('f4', 'twodim', 'm/s'), 
                     Velocity3 = ('f4', 'twodim', 'm/s'), 
                     Velocity4 = ('f4', 'twodim', 'm/s'), 
                     Echo1 = ('f4', 'twodim', 'dB'), 
                     Echo2 = ('f4', 'twodim', 'dB'), 
                     Echo3 = ('f4', 'twodim', 'dB'), 
                     Echo4 = ('f4', 'twodim', 'dB'), 
                     Echo_AVG = ('f4', 'twodim', 'dB'),
                     #
                     BTVel1 = ('f4', 'onedim', 'm/s'),
                     BTVel2 = ('f4', 'onedim', 'm/s'),
                     BTVel3 = ('f4', 'onedim', 'm/s'),
                     BTVel4 = ('f4', 'onedim', 'm/s'), 
                     )
    SECTIONS = 'fixed_leader variable_leader velocity echo bottom_track'.split()
                                    
                                    
    def __init__(self, output_file=None, ensemble_size_limit=None):
        ''' Constructor

        Parameters:
        -----------
        output_file: string representing the output filename
        ensemble_size_limit: integer | None limiting the number of ensembles to be written into 
                             a single netcdf file. None or 0 means no limit (one file will be written).
        '''
        
        super().__init__()
        self.ensemble_size_limit = ensemble_size_limit
        self.file_counter = 0
        self.ensemble_counter = 0
        self.output_file = output_file
        self.coro_fun = self.coro_write_ensembles()
        
    def close(self):
        ''' Close current open file '''
        self.dataset.close()

    def enter(self):
        # open a file when we enter via the context manager
        self.open()
        
    def exit(self, type, value, tb):
        if type is None:
            # no error on leaving the context manager
            self.close()
            
    @coroutine
    def coro_write_ensembles(self):
        while True:
            try:
                ens = (yield)
            except GeneratorExit:
                break
            else:
                if self.ensemble_counter == 0:
                    dimensions, variables = self.initialise(ens)
                self.add_ensemble(ens, variables)
                if self.ensemble_size_limit and self.ensemble_counter==self.ensemble_size_limit:
                    self.open()
        if not self.is_context_manager:
            # if we leave the coroutine, only close the file if we are not in a context manager.
            self.close()
        
    # "private" methods
    def create_dimensions(self, n_bins):
        time = self.dataset.createDimension('time', None)
        lat = self.dataset.createDimension('lat',1)
        lon = self.dataset.createDimension('lon',1)
        z = self.dataset.createDimension('z', n_bins)
        
        return time, z, lat, lon
    
    def create_variables(self, ens):
        variables =dict()
        variables['time'] = self.dataset.createVariable('time', 'f8', ('time',))
        variables['time'].units = 's'
        variables['time'].long_name = 'time'
        variables['time'].standard_name = 'time'

        
        variables['z'] = self.dataset.createVariable('z', 'f4', ('z',))
        variables['z'].units = 'm'
        z = np.arange(ens['fixed_leader']['N_Cells'])*ens['fixed_leader']['DepthCellSize']
        z += ens['fixed_leader']['FirstBin']
        variables['z'][...] = z

        for s, grp in ens.items():
            if s not in self.SECTIONS:
                continue
            for v, value in grp.items():
                try:
                    fmt, dim, units = self.VARIABLES[v]
                except KeyError:
                    pass
                else:
                    if dim == 'scalar':
                        variables[v] = self.dataset.createVariable(v, fmt)
                        variables[v][...] = value
                    elif dim == 'onedim':
                        variables[v] = self.dataset.createVariable(v, fmt, ('time',))
                    elif dim == 'twodim':
                        variables[v] = self.dataset.createVariable(v, fmt, ('time','z'))
                    else:
                        raise ValueError('Unknown Dimension specification.')
                    variables[v].units = units
        return variables

    def add_ensemble(self, ens, variables):
        k = self.ensemble_counter
        for s, grp in ens.items():
            if s not in self.SECTIONS:
                continue
            for v, value in grp.items():
                try:
                    fmt, dim, units = self.VARIABLES[v]
                except KeyError:
                    pass
                else:
                    if dim == 'onedim':
                        variables[v][k] = value
                    elif dim == 'twodim':
                        variables[v][k,...] = value
        try:
            variables['time'][k] = ens['variable_leader']['Timestamp']
        except KeyError:
            variables['time'][k] = get_ensemble_time(ens)
        self.ensemble_counter+=1
        
    def initialise(self, ens):
        n_bins = ens['fixed_leader']['N_Cells']
        dimensions = self.create_dimensions(n_bins)
        variables = self.create_variables(ens)
        return dimensions, variables

    def open(self):
        if self.ensemble_size_limit:
            base, ext = os.path.splitext(self.output_file)
            output_file = "{:s}{:04d}{:s}".format(base, self.file_counter, ext)
            self.file_counter+=1
        else:
            output_file=self.output_file
        try:
            self.close()
        except:
            pass
        self.dataset = Dataset(output_file, 'w', format = "NETCDF4")
        self.ensemble_counter = 0
        

class AsciiWriter(Writer):
    DESCRIPTIONS = {"Earth":"Eastward current-Northward current-Upward current-Error velocity".split("-"),
                    "Beam" :"Beam 1-Beam 2-Beam 3-Beam 4".split("-"),
                    "Ship" :"Starboard current-Forward current-Upward current-Error velocity".split("-"),
                    "Instrument": "Current in x direction-Current in y direction-Current in z direction-error velocity".split("-")}
                    
    def __init__(self, output_file = sys.stdout, adcp_offset=0):
        '''AsciiWriter
        
        Parameters:
        -----------
        output_file: a file pointer of filename (default sys.stdout)
        adcp_offset: the distance in m that the profile data should be offset by

        '''
        
        super().__init__()
        self.comment = "#"
        self.adcp_offset = adcp_offset
        self.coro_fun = self.coro_write_ensembles(output_file)
  
    def write_configuration(self, config, fd=sys.stdout):
        fd.write("{}Configuration:\n".format(self.comment))
        for _, v in self.parameters['config']:
            try:
                fd.write("{}{} : {}\n".format(self.comment, v, config[v]))
            except KeyError:
                pass
        fd.write("{}\n".format(self.comment))
                     
    def write_header(self, config, fd=sys.stdout):
        ustr, vstr, wstr, verrstr = AsciiWriter.DESCRIPTIONS[config['CoordXfrm']]
        fd.write("{}{:19s}{:<20s}{:<20s}{:<20s}{:<20s}{:<20s}\n".format(self.comment, "Time", "Elevation", ustr, vstr, wstr, verrstr))
        fd.write("{}{:19s}{:<20s}{:<20s}{:<20s}{:<20s}{:<20s}\n".format(self.comment, "Date/Time", "m", "m/s", "m/s", "m/s", "m/s"))
        fd.write("{}\n".format(self.comment))
        
    def write_array(self, config, data1d, data2d, fd=sys.stdout):
        firstbin = config['FirstBin']
        binsize = config['DepthCellSize']
        factor = (int(config['Xdcr_Facing']=='Up')*2-1)
        
        for t, u, v, w, verr in zip(data1d['Time'], data2d['velocity Velocity1'], data2d['velocity Velocity2'], data2d['velocity Velocity3'], data2d['velocity Velocity4']):
            dt = datetime.datetime.utcfromtimestamp(t)
            tstr = dt.strftime("%Y-%m-%dT%H:%M:%S")
            for i, p in enumerate(zip(u, v, w, verr)):
                r = firstbin + i*binsize
                z= factor*r + self.adcp_offset
                try:
                    fd.write("{:20s}{:< 20.3f}{:< 20.3f}{:< 20.3f}{:< 20.3f}{:< 20.3f}\n".format(tstr, z, *p))
                except TypeError:
                    pass
        
class NDFWriter(Writer):
    def __init__(self, output_file = None):
        super().__init__()
        self._global_parameters = dict()
        self.output_file = output_file
        self.coro_fun = self.coro_write_ensembles()
        
    @coroutine
    def coro_write_ensembles(self):
        ''' a non-lazy implementation. This reads all the data into memory because of
            how ndf files are written. NDF files cannot be written from generators.
        '''
        data1d = defaultdict(lambda : [])
        data2d = defaultdict(lambda : [])
        self.__cache = dict(config=False, data1d=data1d, data2d=data2d)
        while True:
            try:
                ens = (yield)
            except GeneratorExit:
                break
            else:
                if not self.__cache['config']:
                    self.__cache['config']= ens['fixed_leader']
                self.read_scalar_data(data1d, ens)
                self.read_vector_data(data2d, ens)
        if not self.is_context_manager:
            self.write_to_file()

    def exit(self, type, value, tb):
        self.is_context_manager=False
        if type is None:
            self.write_to_file()
            self.__cache['config']=False
            self.__cache['data1d'].clear()
            self.__cache['data2d'].clear()
            
    def write_to_file(self):
        config = self.__cache['config']
        if config:
            data1d = self.__cache['data1d']
            data2d = self.__cache['data2d']
            data = self.create_ndf(config, data1d, data2d)
            data.save(self.output_file)


    def set_filename_from_pd0(self, filename_pd0,annotation=None):
        fn_base, fn_ext = os.path.splitext(filename_pd0)
        if annotation:
            self.output_file =  "{}-{}.ndf".format(fn_base,annotation)
        else:
            self.output_file =  "{}.ndf".format(fn_base)

        
    def add_global_parameter(self, key, value, unit):
        self._global_parameters[key] = value, unit
        
    
    def create_ndf(self, config, data1d, data2d):
        units=defaultdict(lambda : '-', Soundspeed='m/s', Temperature='degree', Depth='m',
                          Velocity1='m/s',Velocity2='m/s',Velocity3='m/s',Velocity4='m/s',
                          BTVel1='m/s', BTVel2='m/s', BTVel3='m/s', BTVel4='m/s',
                          Echo1='dB',Echo2='dB',Echo3='dB',Echo4='dB', Echo_AVG='dB',
                          Beam_Angle='deg', DepthCellSize='m',Blank='m',ErrVelThreshold='m/s',
                          FirstBin='m',XmtLegnth='m', LagDistance='m')
        data = ndf.NDF()
        tm = np.array(data1d['Time'])
        n_cells = config['N_Cells']
        bin_size = config['DepthCellSize']
        z = np.arange(n_cells)*bin_size + config['FirstBin']
        for k, v in data1d.items():
            if k == 'Time':
                continue
            if "BTVel" in k:
                i = int(k[-1])
                s = TransformationTranslations[config['CoordXfrm']][i-1]
                ks = k.replace("BTVel%d"%(i), s)
            else:
                ks = k
            v = self.array1d_from_list(v)
            data.add_parameter(ks, units[k], (tm, v))
        for k, v in data2d.items():
            if "Velocity" in k:
                i = int(k[-1])
                s = TransformationTranslations[config['CoordXfrm']][i-1]
                ks = k.replace("Velocity%d"%(i), s)
            else:
                ks = k
            v = self.array2d_from_list(v)
            data.add_parameter(ks, units[k], (tm,z,v.T))
        for k, v in config.items():
            if isinstance(v, str):
                data.add_metadata(k,v)
            else:
                data.add_global_parameter(k, v, units[k])
        # add any metadata present
        for k, v in self._global_parameters.items():
            data.add_global_parameter(k,*v)
        return data


class DataStructure(Writer):
    ''' Simple in memory data structure '''
    def __init__(self):
        super().__init__()
        self.data = defaultdict(lambda : [])
        self.coro_fun = self.coro_write_ensembles(fd=None)
        
    def __getattr__(self, item):
        if item in self.data.keys():
            if isinstance(self.data[item], list):
                if self.data[item]:
                    if isinstance(self.data[item][0], float):
                        self.data[item] = np.ma.hstack(self.data[item])
                    else:
                        self.data[item] = np.ma.vstack(self.data[item])
            return self.data[item]
        else:
            raise AttributeError("%s has no attribute '%s'."%(self, item))
            
    def write_configuration(self, config, fd):
        pass
    
    def write_header(self, config, fd):
        pass

    def write_array(self, config, scalar_data, vector_data, fd):
        transform = config['CoordXfrm']
        for k, v in chain(scalar_data.items(), vector_data.items()):
            try:
                s, m = k.split()
            except ValueError:
                ks = k
            else:
                if (s=='velocity' or s=='bottom_track') and ("Vel" in m):
                    i = int(m[-1])
                    t = TransformationTranslations[transform][i-1]
                    ks = "_".join((s, t))
                else:
                    ks = k.replace(" ", "_")
            self.data[ks]+=v
