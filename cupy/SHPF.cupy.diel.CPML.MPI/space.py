import time, os, datetime, sys, ctypes
import numpy as xp
import cupy as cp
import matplotlib.pyplot as plt
from mpi4py import MPI
from mpl_toolkits.mplot3d import axes3d
from mpl_toolkits.axes_grid1 import make_axes_locatable
from scipy.constants import c, mu_0, epsilon_0

class Basic3D(object):
    
    def __init__(self, grid, gridgap, dt, tsteps, dtype, **kwargs):
        """Create Simulation Space.

            ex) Space.grid((128,128,600), (50*nm,50*nm,5*nm), dtype=xp.complex64)

        PARAMETERS
        ----------
        grid : tuple
            define the x,y,z grid.

        gridgap : tuple
            define the dx, dy, dz.

        dtype : class numpy dtype
            choose xp.complex64 or xp.complex128

        kwargs : string
            
            supported arguments
            -------------------

            courant : float
                Set the courant number. For HPF, default is 1./4 and for FDTD, default is 1./2

        RETURNS
        -------
        None
        """

        self.nm = 1e-9
        self.um = 1e-6  

        self.dtype    = dtype
        self.MPIcomm  = MPI.COMM_WORLD
        self.MPIrank  = self.MPIcomm.Get_rank()
        self.MPIsize  = self.MPIcomm.Get_size()
        self.hostname = MPI.Get_processor_name()

        assert len(grid)    == 3, "Simulation grid should be a tuple with length 3."
        assert len(gridgap) == 3, "Argument 'gridgap' should be a tuple with length 3."

        self.tsteps = tsteps        

        self.grid = grid
        self.Nx = self.grid[0]
        self.Ny = self.grid[1]
        self.Nz = self.grid[2]
        self.TOTAL_NUM_GRID = self.Nx * self.Ny * self.Nz
        self.TOTAL_NUM_GRID_SIZE = (self.dtype(1).nbytes * self.TOTAL_NUM_GRID) / 1024 / 1024
        
        self.Nxc = int(self.Nx / 2)
        self.Nyc = int(self.Ny / 2)
        self.Nzc = int(self.Nz / 2)
        
        self.gridgap = gridgap
        self.dx = self.gridgap[0]
        self.dy = self.gridgap[1]
        self.dz = self.gridgap[2]

        self.Lx = self.Nx * self.dx
        self.Ly = self.Ny * self.dy
        self.Lz = self.Nz * self.dz

        self.VOLUME = self.Lx * self.Ly * self.Lz

        if self.MPIrank == 0:
            print("VOLUME of the space: {:.2e}" .format(self.VOLUME))
            print("Number of grid points: {:5d} x {:5d} x {:5d}" .format(self.Nx, self.Ny, self.Nz))
            print("Grid spacing: {:.3f} nm, {:.3f} nm, {:.3f} nm" .format(self.dx/self.nm, self.dy/self.nm, self.dz/self.nm))

        self.MPIcomm.Barrier()

        self.courant = 1./4

        if kwargs.get('engine') != None: self.engine = kwargs.get('engine')
        if kwargs.get('courant') != None: self.courant = kwargs.get('courant')

        assert self.engine == np or self.engine == cp

        self.dt = dt
        self.maxdt = 1. / c / np.sqrt( (1./self.dx)**2 + (1./self.dy)**2 + (1./self.dz)**2 )

        assert (c * self.dt * np.sqrt( (1./self.dx)**2 + (1./self.dy)**2 + (1./self.dz)**2 )) < 1.

        """
        For more details about maximum dt in the Hybrid PSTD-FDTD method, see
        Combining the FDTD and PSTD methods, Y.F.Leung, C.H. Chan,
        Microwave and Optical technology letters, Vol.23, No.4, November 20 1999.
        """

        self.myPMLregion_x = None
        self.myPMLregion_y = None
        self.myPMLregion_z = None
        self.myPBCregion_x = False
        self.myPBCregion_y = False
        self.myPBCregion_z = False
        self.myBBCregion_x = False
        self.myBBCregion_y = False
        self.myBBCregion_z = False

        assert self.dt < self.maxdt, "Time interval is too big so that causality is broken. Lower the courant number."
        assert float(self.Nx) % self.MPIsize == 0., "Nx must be a multiple of the number of nodes."
        
        ############################################################################
        ################# Set the loc_grid each node should possess ################
        ############################################################################

        xp = self.engine

        self.myNx     = int(self.Nx/self.MPIsize)
        self.loc_grid = (self.myNx, self.Ny, self.Nz)

        self.Ex = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.Ey = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.Ez = xp.zeros(self.loc_grid, dtype=self.dtype)

        self.Hx = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.Hy = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.Hz = xp.zeros(self.loc_grid, dtype=self.dtype)
        ###############################################################################

        self.ky = xp.fft.fftfreq(self.Ny, self.dy) * 2 * xp.pi
        self.kz = xp.fft.fftfreq(self.Nz, self.dz) * 2 * xp.pi

        self.diffxEy = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.diffxEz = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.diffyEx = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.diffyEz = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.diffzEx = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.diffzEy = xp.zeros(self.loc_grid, dtype=self.dtype)

        self.diffxHy = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.diffxHz = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.diffyHx = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.diffyHz = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.diffzHx = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.diffzHy = xp.zeros(self.loc_grid, dtype=self.dtype)
        ############################################################################

        self.eps_Ex = xp.ones(self.loc_grid, dtype=self.dtype) * epsilon_0
        self.eps_Ey = xp.ones(self.loc_grid, dtype=self.dtype) * epsilon_0
        self.eps_Ez = xp.ones(self.loc_grid, dtype=self.dtype) * epsilon_0

        self.mu_Hx  = xp.ones(self.loc_grid, dtype=self.dtype) * mu_0
        self.mu_Hy  = xp.ones(self.loc_grid, dtype=self.dtype) * mu_0
        self.mu_Hz  = xp.ones(self.loc_grid, dtype=self.dtype) * mu_0

        self.econ_Ex = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.econ_Ey = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.econ_Ez = xp.zeros(self.loc_grid, dtype=self.dtype)

        self.mcon_Hx = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.mcon_Hy = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.mcon_Hz = xp.zeros(self.loc_grid, dtype=self.dtype)

        ###############################################################################
        ####################### Slices of xgrid that each node got ####################
        ###############################################################################
        
        self.myNx_slices = []
        self.myNx_indice = []

        for rank in range(self.MPIsize):

            xsrt = (rank  ) * self.myNx
            xend = (rank+1) * self.myNx

            self.myNx_slices.append(slice(xsrt, xend))
            self.myNx_indice.append(     (xsrt, xend))

        self.MPIcomm.Barrier()
        #print("rank {:>2}:\tmy xindex: {},\tmy xslice: {}" \
        #       .format(self.MPIrank, self.myNx_indice[self.MPIrank], self.myNx_slices[self.MPIrank]))

    def set_PML(self, region, npml):

        self.PMLregion  = region
        self.npml       = npml
        self.PMLgrading = 2 * self.npml

        self.rc0   = 1.e-16                             # reflection coefficient
        self.imp   = xp.sqrt(mu_0/epsilon_0)            # impedence
        self.gO    = 3.                                 # gradingOrder
        self.sO    = 3.                                 # scalingOrder
        self.bdw_x = (self.PMLgrading-1) * self.dx      # PML thickness along x (Boundarywidth)
        self.bdw_y = (self.PMLgrading-1) * self.dy      # PML thickness along y
        self.bdw_z = (self.PMLgrading-1) * self.dz      # PML thickness along z

        self.PMLsigmamaxx = -(self.gO+1) * xp.log(self.rc0) / (2*self.imp*self.bdw_x)
        self.PMLsigmamaxy = -(self.gO+1) * xp.log(self.rc0) / (2*self.imp*self.bdw_y)
        self.PMLsigmamaxz = -(self.gO+1) * xp.log(self.rc0) / (2*self.imp*self.bdw_z)

        self.PMLkappamaxx = 1.
        self.PMLkappamaxy = 1.
        self.PMLkappamaxz = 1.

        self.PMLalphamaxx = 0.02
        self.PMLalphamaxy = 0.02
        self.PMLalphamaxz = 0.02

        self.PMLsigmax = xp.zeros(self.PMLgrading, dtype=self.dtype)
        self.PMLalphax = xp.zeros(self.PMLgrading, dtype=self.dtype)
        self.PMLkappax = xp.ones (self.PMLgrading, dtype=self.dtype)

        self.PMLsigmay = xp.zeros(self.PMLgrading, dtype=self.dtype)
        self.PMLalphay = xp.zeros(self.PMLgrading, dtype=self.dtype)
        self.PMLkappay = xp.ones (self.PMLgrading, dtype=self.dtype)

        self.PMLsigmaz = xp.zeros(self.PMLgrading, dtype=self.dtype)
        self.PMLalphaz = xp.zeros(self.PMLgrading, dtype=self.dtype)
        self.PMLkappaz = xp.ones (self.PMLgrading, dtype=self.dtype)

        self.PMLbx = xp.zeros(self.PMLgrading, dtype=self.dtype)
        self.PMLby = xp.zeros(self.PMLgrading, dtype=self.dtype)
        self.PMLbz = xp.zeros(self.PMLgrading, dtype=self.dtype)

        self.PMLax = xp.zeros(self.PMLgrading, dtype=self.dtype)
        self.PMLay = xp.zeros(self.PMLgrading, dtype=self.dtype)
        self.PMLaz = xp.zeros(self.PMLgrading, dtype=self.dtype)

        #------------------------------------------------------------------------------------------------#
        #------------------------------- Grading kappa, sigma and alpha ---------------------------------#
        #------------------------------------------------------------------------------------------------#

        for key, value in self.PMLregion.items():

            if   key == 'x' and value != '':

                self.psi_eyx_p = xp.zeros((npml, self.Ny, self.Nz), dtype=self.dtype)
                self.psi_ezx_p = xp.zeros((npml, self.Ny, self.Nz), dtype=self.dtype)
                self.psi_hyx_p = xp.zeros((npml, self.Ny, self.Nz), dtype=self.dtype)
                self.psi_hzx_p = xp.zeros((npml, self.Ny, self.Nz), dtype=self.dtype)

                self.psi_eyx_m = xp.zeros((npml, self.Ny, self.Nz), dtype=self.dtype)
                self.psi_ezx_m = xp.zeros((npml, self.Ny, self.Nz), dtype=self.dtype)
                self.psi_hyx_m = xp.zeros((npml, self.Ny, self.Nz), dtype=self.dtype)
                self.psi_hzx_m = xp.zeros((npml, self.Ny, self.Nz), dtype=self.dtype)

                """
                for i in range(self.PMLgrading):

                    loc  = xp.float64(i) * self.dx / self.bdw_x

                    self.PMLsigmax[i] = self.PMLsigmamaxx * (loc **self.gO)
                    self.PMLkappax[i] = 1 + ((self.PMLkappamaxx-1) * (loc **self.gO))
                    self.PMLalphax[i] = self.PMLalphamaxx * ((1-loc) **self.sO)
                """
                loc = xp.arange(self.PMLgrading) * self.dx / self.bdw_x
                self.PMLsigmax = self.PMLsigmamaxx * (loc **self.gO)
                self.PMLkappax = 1 + ((self.PMLkappamaxx-1) * (loc **self.gO))
                self.PMLalphax = self.PMLalphamaxx * ((1-loc) **self.sO)

            elif key == 'y' and value != '':

                self.psi_exy_p = xp.zeros((self.myNx, npml, self.Nz), dtype=self.dtype)
                self.psi_ezy_p = xp.zeros((self.myNx, npml, self.Nz), dtype=self.dtype)
                self.psi_hxy_p = xp.zeros((self.myNx, npml, self.Nz), dtype=self.dtype)
                self.psi_hzy_p = xp.zeros((self.myNx, npml, self.Nz), dtype=self.dtype)

                self.psi_exy_m = xp.zeros((self.myNx, npml, self.Nz), dtype=self.dtype)
                self.psi_ezy_m = xp.zeros((self.myNx, npml, self.Nz), dtype=self.dtype)
                self.psi_hxy_m = xp.zeros((self.myNx, npml, self.Nz), dtype=self.dtype)
                self.psi_hzy_m = xp.zeros((self.myNx, npml, self.Nz), dtype=self.dtype)
                """
                for i in range(self.PMLgrading):

                    loc  = xp.float64(i) * self.dy / self.bdw_y

                    self.PMLsigmay[i] = self.PMLsigmamaxy * (loc **self.gO)
                    self.PMLkappay[i] = 1 + ((self.PMLkappamaxy-1) * (loc **self.gO))
                    self.PMLalphay[i] = self.PMLalphamaxy * ((1-loc) **self.sO)
                """

                loc  = xp.arange(self.PMLgrading) * self.dy / self.bdw_y
                self.PMLsigmay = self.PMLsigmamaxy * (loc **self.gO)
                self.PMLkappay = 1 + ((self.PMLkappamaxy-1) * (loc **self.gO))
                self.PMLalphay = self.PMLalphamaxy * ((1-loc) **self.sO)

            elif key == 'z' and value != '':

                self.psi_exz_p = xp.zeros((self.myNx, self.Ny, npml), dtype=self.dtype)
                self.psi_eyz_p = xp.zeros((self.myNx, self.Ny, npml), dtype=self.dtype)
                self.psi_hxz_p = xp.zeros((self.myNx, self.Ny, npml), dtype=self.dtype)
                self.psi_hyz_p = xp.zeros((self.myNx, self.Ny, npml), dtype=self.dtype)

                self.psi_exz_m = xp.zeros((self.myNx, self.Ny, npml), dtype=self.dtype)
                self.psi_eyz_m = xp.zeros((self.myNx, self.Ny, npml), dtype=self.dtype)
                self.psi_hxz_m = xp.zeros((self.myNx, self.Ny, npml), dtype=self.dtype)
                self.psi_hyz_m = xp.zeros((self.myNx, self.Ny, npml), dtype=self.dtype)

                """
                for i in range(self.PMLgrading):

                    loc  = xp.float64(i) * self.dz / self.bdw_z

                    self.PMLsigmaz[i] = self.PMLsigmamaxz * (loc **self.gO)
                    self.PMLkappaz[i] = 1 + ((self.PMLkappamaxz-1) * (loc **self.gO))
                    self.PMLalphaz[i] = self.PMLalphamaxz * ((1-loc) **self.sO)
                """

                loc  = xp.arange(selfe.PMLgrading) * self.dz / self.bdw_z
                self.PMLsigmaz = self.PMLsigmamaxz * (loc **self.gO)
                self.PMLkappaz = 1 + ((self.PMLkappamaxz-1) * (loc **self.gO))
                self.PMLalphaz = self.PMLalphamaxz * ((1-loc) **self.sO)

        #------------------------------------------------------------------------------------------------#
        #--------------------------------- Get 'b' and 'a' for CPML theory ------------------------------#
        #------------------------------------------------------------------------------------------------#

        if 'x' in self.PMLregion.keys() and self.PMLregion.get('x') != '':
            self.PMLbx = xp.exp(-(self.PMLsigmax/self.PMLkappax + self.PMLalphax) * self.dt / epsilon_0)
            self.PMLax = self.PMLsigmax / (self.PMLsigmax*self.PMLkappax + self.PMLalphax*self.PMLkappax**2) * (self.PMLbx - 1.)

        if 'y' in self.PMLregion.keys() and self.PMLregion.get('y') != '':
            self.PMLby = xp.exp(-(self.PMLsigmay/self.PMLkappay + self.PMLalphay) * self.dt / epsilon_0)
            self.PMLay = self.PMLsigmay / (self.PMLsigmay*self.PMLkappay + self.PMLalphay*self.PMLkappay**2) * (self.PMLby - 1.)

        if 'z' in self.PMLregion.keys() and self.PMLregion.get('z') != '':
            self.PMLbz = xp.exp(-(self.PMLsigmaz/self.PMLkappaz + self.PMLalphaz) * self.dt / epsilon_0)
            self.PMLaz = self.PMLsigmaz / (self.PMLsigmaz*self.PMLkappaz + self.PMLalphaz*self.PMLkappaz**2) * (self.PMLbz - 1.)

        return

    def save_pml_parameters(self, path):
        """Save PML parameters to check"""

        if self.MPIrank == 0:
            try: import h5py
            except ImportError as e:
                print("Please install h5py and hdfviewer")
                return
            
            f = h5py.File(path+'pml_parameters.h5', 'w')

            for key,value in self.PMLregion.items():
                if key == 'x':
                    f.create_dataset('PMLsigmax' ,  data=self.PMLsigmax)
                    f.create_dataset('PMLkappax' ,  data=self.PMLkappax)
                    f.create_dataset('PMLalphax' ,  data=self.PMLalphax)
                    f.create_dataset('PMLbx',       data=self.PMLbx)
                    f.create_dataset('PMLax',       data=self.PMLax)
                elif key == 'y':
                    f.create_dataset('PMLsigmay' ,  data=self.PMLsigmay)
                    f.create_dataset('PMLkappay' ,  data=self.PMLkappay)
                    f.create_dataset('PMLalphay' ,  data=self.PMLalphay)
                    f.create_dataset('PMLby',       data=self.PMLby)
                    f.create_dataset('PMLay',       data=self.PMLay)
                elif key == 'z':
                    f.create_dataset('PMLsigmaz' ,  data=self.PMLsigmaz)
                    f.create_dataset('PMLkappaz' ,  data=self.PMLkappaz)
                    f.create_dataset('PMLalphaz' ,  data=self.PMLalphaz)
                    f.create_dataset('PMLbz',       data=self.PMLbz)
                    f.create_dataset('PMLaz',       data=self.PMLaz)

        else: pass
            
        self.MPIcomm.Barrier()
        
        return

    def save_eps_mu(self, path):
        """Save eps_r and mu_r to check

        """

        try: import h5py
        except ImportError as e:
            print("rank {:>2}\tPlease install h5py and hdfviewer to save data." .format(self.MPIrank))
            return
        save_dir = path+'eps_mu/'       

        if os.path.exists(save_dir) == False: os.mkdir(save_dir)
        else: pass

        f = h5py.File(save_dir+'eps_r_mu_r_rank{:>02d}.h5' .format(self.MPIrank), 'w')

        f.create_dataset('eps_Ex',  data=self.eps_Ex)
        f.create_dataset('eps_Ey',  data=self.eps_Ey)
        f.create_dataset('eps_Ez',  data=self.eps_Ez)
        f.create_dataset( 'mu_Hx',  data=self. mu_Hx)
        f.create_dataset( 'mu_Hy',  data=self. mu_Hy)
        f.create_dataset( 'mu_Hz',  data=self. mu_Hz)
            
        self.MPIcomm.Barrier()

        return

    def apply_PBC(self, region):
        """Specify the boundary to apply Periodic Boundary Condition.

        PARAMETERS
        ----------
        region : dictionary
            ex) {'x':'','y':'+-','z':'+-'}

        RETURNS
        -------
        None
        """

        value = region.get('x')
        if value == '+-' or value == '-+':
            if self.MPIsize > 1:
                if   self.MPIrank == 0               : self.myPBCregion_x = '-'
                elif self.MPIrank == (self.MPIsize-1): self.myPBCregion_x = '+'
        elif value == None: pass
        else: raise ValueError("The value of key 'x' should be None or '+-' or '-+'.")

        value = region.get('y')
        if   value == True:  self.myPBCregion_y = True
        elif value == False: self.myPBCregion_y = False
        else: raise ValueError("Choose True or False")

        value = region.get('z')
        if   value == True:  self.myPBCregion_z = True
        elif value == False: self.myPBCregion_z = False
        else: raise ValueError("Choose True or False")

        """
        for key, value in region.items():

            if   key == 'x':

                if   value == '+': raise ValueError("input '+-' or '-+'.")
                elif value == '-': raise ValueError("input '+-' or '-+'.")
                elif value == '+-' or value == '-+':

                    if   self.MPIrank == 0               : self.myPBCregion_x = '-'
                    elif self.MPIrank == (self.MPIsize-1): self.myPBCregion_x = '+'

            elif key == 'y':

                if value == True: self.myPBCregion_y = True
                elif value == False: self.myPBCregion_y = False
                else: raise ValueError("Choose True or False")

            elif key == 'z':
    
                if value == True: self.myPBCregion_z = True
                elif value == False: self.myPBCregion_z = False
                else: raise ValueError("Choose True or False")
        """

        self.MPIcomm.Barrier()
        #print("PBC region of rank: {}, x: {}, y: {}, z: {}" \
        #       .format(self.MPIrank, self.myPBCregion_x, self.myPBCregion_y, self.myPBCregion_z))

    def apply_BBC(self, region):
        """Specify the boundary to apply Bloch Boundary Condition.

        PARAMETERS
        ----------
        region : dictionary
            ex) {'x':'','y':'+-','z':'+-'}

        RETURNS
        -------
        None
        """

        value = region.get('x')
        if value == '+-' or value == '-+':
            if self.MPIsize > 1:
                if   self.MPIrank == 0               : self.myBBCregion_x = '-'
                elif self.MPIrank == (self.MPIsize-1): self.myBBCregion_x = '+'
        elif value == None: pass
        else: raise ValueError("The value of key 'x' should be None or '+-' or '-+'.")

        value = region.get('y')
        if   value == True:  self.myBBCregion_y = True
        elif value == False: self.myBBCregion_y = False
        else: raise ValueError("Choose True or False")

        value = region.get('z')
        if   value == True:  self.myBBCregion_z = True
        elif value == False: self.myBBCregion_z = False
        else: raise ValueError("Choose True or False")

        """
        for key, value in region.items():

            if   key == 'x':

                if   value == '+': raise ValueError("input '+-' or '-+'.")
                elif value == '-': raise ValueError("input '+-' or '-+'.")
                elif value == '+-' or value == '-+':

                    if   self.MPIrank == 0               : self.myBBCregion_x = '-'
                    elif self.MPIrank == (self.MPIsize-1): self.myBBCregion_x = '+'

            elif key == 'y':

                if value == True: self.myBBCregion_y = True
                elif value == False: self.myBBCregion_y = False
                else: raise ValueError("Choose True or False")

            elif key == 'z':
    
                if value == True: self.myBBCregion_z = True
                elif value == False: self.myBBCregion_z = False
                else: raise ValueError("Choose True or False")
        """
        self.MPIcomm.Barrier()

    def set_src_pos(self, src_srt, src_end):
        """Set the position, type of the source and field.

        PARAMETERS
        ----------
        src_srt : tuple
        src_end   : tuple

            A tuple which has three ints as its elements.
            The elements defines the position of the source in the field.
            
            ex)
                1. point source
                    src_srt: (30, 30, 30), src_end: (30, 30, 30)
                2. line source
                    src_srt: (30, 30, 0), src_end: (30, 30, Space.Nz)
                3. plane wave
                    src_srt: (30,0,0), src_end: (30, Space.Ny, Space.Nz)

        RETURNS
        -------
        None
        """

        assert len(src_srt) == 3, "src_srt argument is a list or tuple with length 3."
        assert len(src_end) == 3, "src_end argument is a list or tuple with length 3."

        self.who_put_src = None

        self.src_srt  = src_srt
        self.src_xsrt = src_srt[0]
        self.src_ysrt = src_srt[1]
        self.src_zsrt = src_srt[2]

        self.src_end  = src_end
        self.src_xend = src_end[0]
        self.src_yend = src_end[1]
        self.src_zend = src_end[2]

        #----------------------------------------------------------------------#
        #--------- All rank should know who put src to plot src graph ---------#
        #----------------------------------------------------------------------#

        self.MPIcomm.Barrier()
        for rank in range(self.MPIsize):

            my_xsrt = self.myNx_indice[rank][0]
            my_xend = self.myNx_indice[rank][1]

            # case 1. x position of source is fixed.
            if self.src_xsrt == (self.src_xend-1):

                if self.src_xsrt >= my_xsrt and self.src_xend <= my_xend:
                    self.who_put_src   = rank

                    if self.MPIrank == self.who_put_src:
                        self.my_src_xsrt = self.src_xsrt - my_xsrt
                        self.my_src_xend = self.src_xend - my_xsrt

                        self.src = xp.zeros(self.tsteps, dtype=self.dtype)

                        #print("rank{:>2}: src_xsrt : {}, my_src_xsrt: {}, my_src_xend: {}"\
                        #       .format(self.MPIrank, self.src_xsrt, self.my_src_xsrt, self.my_src_xend))
                    else:
                        pass
                        #print("rank {:>2}: I don't put source".format(self.MPIrank))

                else: continue

            # case 2. x position of source has range.
            elif self.src_xsrt < self.src_xend:
                assert self.MPIsize == 1

                self.who_put_src = 0
                self.my_src_xsrt = self.src_xsrt
                self.my_src_xend = self.src_xend

                self.src = xp.zeros(self.tsteps, dtype=self.dtype)

            # case 3. x position of source is reversed.
            elif self.src_xsrt > self.src_xend:
                raise ValueError("src_end[0] is bigger than src_srt[0]")

            else:
                raise IndexError("x position of src is not defined!")

    def put_src(self, where, pulse, put_type):
        """Put source at the designated postion set by set_src_pos method.
        
        PARAMETERS
        ----------  
        where : string
            ex)
                'Ex' or 'ex'
                'Ey' or 'ey'
                'Ez' or 'ez'

        pulse : float
            float returned by source.pulse.

        put_type : string
            'soft' or 'hard'

        """
        #------------------------------------------------------------#
        #--------- Put the source into the designated field ---------#
        #------------------------------------------------------------#

        self.put_type = put_type

        self.where = where
        
        self.pulse = self.dtype(pulse)

        if self.MPIrank == self.who_put_src:

            x = slice(self.my_src_xsrt, self.my_src_xend)
            y = slice(self.   src_ysrt, self.   src_yend)
            z = slice(self.   src_zsrt, self.   src_zend)
            
            if   self.put_type == 'soft':

                if (self.where == 'Ex') or (self.where == 'ex'): self.Ex[x,y,z] += self.pulse
                if (self.where == 'Ey') or (self.where == 'ey'): self.Ey[x,y,z] += self.pulse
                if (self.where == 'Ez') or (self.where == 'ez'): self.Ez[x,y,z] += self.pulse
                if (self.where == 'Hx') or (self.where == 'hx'): self.Hx[x,y,z] += self.pulse
                if (self.where == 'Hy') or (self.where == 'hy'): self.Hy[x,y,z] += self.pulse
                if (self.where == 'Hz') or (self.where == 'hz'): self.Hz[x,y,z] += self.pulse

            elif self.put_type == 'hard':
    
                if (self.where == 'Ex') or (self.where == 'ex'): self.Ex[x,y,z] = self.pulse
                if (self.where == 'Ey') or (self.where == 'ey'): self.Ey[x,y,z] = self.pulse
                if (self.where == 'Ez') or (self.where == 'ez'): self.Ez[x,y,z] = self.pulse
                if (self.where == 'Hx') or (self.where == 'hx'): self.Hx[x,y,z] = self.pulse
                if (self.where == 'Hy') or (self.where == 'hy'): self.Hy[x,y,z] = self.pulse
                if (self.where == 'Hz') or (self.where == 'hz'): self.Hz[x,y,z] = self.pulse

            else:
                raise ValueError("Please insert 'soft' or 'hard'")

    def updateH(self,tstep) :
        
        #--------------------------------------------------------------#
        #------------ MPI send Ex and Ey to previous rank -------------#
        #--------------------------------------------------------------#

        if (self.MPIrank > 0) and (self.MPIrank < self.MPIsize):

            if self.engine == 'cupy':
                sendEyfirst = cp.asnumpy(self.Ey[0,:,:])
                sendEzfirst = cp.asnumpy(self.Ez[0,:,:])

            else: # engine is numpy.
                sendEyfirst = self.Ey[0,:,:].copy()
                sendEzfirst = self.Ez[0,:,:].copy()

            self.MPIcomm.send( sendEyfirst, dest=(self.MPIrank-1), tag=(tstep*100+9 ))
            self.MPIcomm.send( sendEzfirst, dest=(self.MPIrank-1), tag=(tstep*100+11))

        else: pass

        #-----------------------------------------------------------#
        #------------ MPI recv Ex and Ey from next rank ------------#
        #-----------------------------------------------------------#

        if (self.MPIrank > (-1)) and (self.MPIrank < (self.MPIsize-1)):

            recvEylast = self.MPIcomm.recv( source=(self.MPIrank+1), tag=(tstep*100+9 ))
            recvEzlast = self.MPIcomm.recv( source=(self.MPIrank+1), tag=(tstep*100+11))

            if self.engine == 'cupy':
                recvEylast = cp.asarray(recvEylast)
                recvEzlast = cp.asarray(recvEzlast)

        else: pass

        #-----------------------------------------------------------#
        #---------------------- Get derivatives --------------------#
        #-----------------------------------------------------------#

        if self.engine == 'cupy'
            iky = xp.expand_dims(1j*self.ky, 1)
            ikz = xp.expand_dims(1J*self.kz, 2)
            yshifter = xp.expand_dims(xp.exp(1j*self.ky*self.dy/2), 1)
            zshifter = xp.expand_dims(xp.exp(1j*self.kz*self.dz/2), 2)
        else:
            nax = np.newaxis
            iky = 1j*self.ky[:,nax,:]
            ikz = 1j*self.kz[:,:,nax]
            yshifter = xp.exp(1j*self.ky*self.dy/2)[:,nax,:]
            zshifter = xp.exp(1j*self.kz*self.dz/2)[:,:,nax]

        # To update Hx
        #self.diffyEz[:,:-1,:-1] = (self.Ez[:,1:,:-1] - self.Ez[:,:-1,:-1]) / self.dy
        #self.diffzEy[:,:-1,:-1] = (self.Ez[:,:-1,1:] - self.Ez[:,:-1,:-1]) / self.dz
        self.diffyEz = xp.fft.ifftn(iky*yshifter*zshifter*xp.fft.fftn(self.Ez, axes=(1,2)), axes=(1,2))
        self.diffzEy = xp.fft.ifftn(ikz*yshifter*zshifter*xp.fft.fftn(self.Ey, axes=(1,2)), axes=(1,2))

        # To update Hy
        #self.diffzEx[:-1,:,:-1] = (self.Ex[:-1,:,1:] - self.Ex[:-1,:,:-1]) / self.dz
        self.diffxEz[:-1,:,:-1] = (self.Ez[1:,:,:-1] - self.Ez[:-1,:,:-1]) / self.dx
        self.diffzEx = xp.fft.ifftn(ikz*zshifter*xp.fft.fftn(self.Ex, axes=(2)), axes=(2))

        # To update Hz
        self.diffxEy[:-1,:-1,:] = (self.Ey[1:,:-1,:] - self.Ey[:-1,:-1,:]) / self.dx
        #self.diffyEx[:-1,:-1,:] = (self.Ex[:-1,1:,:] - self.Ex[:-1,:-1,:]) / self.dx
        self.diffyEx = xp.fft.ifftn(iky*yshifter*xp.fft.fftn(self.Ex, axes=(1)), axes=(1))

        if self.MPIrank >= 0  and self.MPIrank < (self.MPIsize-1):

            # No need to update diffzEx and diffyEx because they are already done.
            # To update Hy at x=myNx-1.
            #self.diffzEx[myNx-1,:,:-1] = (self.Ex[myNx-1,:,1:] - self.Ex[myNx-1,:,:-1]) / self.dz
            self.diffxEz[myNx-1,:,:-1] = ( recvEzlast[:,:-1] - self.Ez[myNx-1,:,:-1]) / self.dx

            # To update Hz at x=myNx-1
            self.diffxEy[myNx-1,:-1,:] = (recvEylast[:-1,:] - self.Ey[myNx-1,:-1,:]) / self.dx
            #self.diffyEx[:-1,:-1,:] = (self.Ex[:-1,1:,:] - self.Ex[:-1,:-1,:]) / self.dx

            """
            self.clib_core.get_diff_of_E_rankFM(\
                                                self.myNx, self.Ny, self.Nz,\
                                                self.dt, self.dx, self.dy, self.dz, \
                                                recvEylast, 
                                                recvEzlast, 
                                                self.Ex, 
                                                self.Ey, 
                                                self.Ez, 
                                                self.diffxEy, 
                                                self.diffxEz, 
                                                self.diffyEx, 
                                                self.diffyEz, 
                                                self.diffzEx, 
                                                self.diffzEy
                                                )
            """
        elif self.MPIrank == (self.MPIsize-1): pass

            """
            self.clib_core.get_diff_of_E_rank_L(\
                                                self.myNx, self.Ny, self.Nz,\
                                                self.dt, self.dx, self.dy, self.dz, \
                                                self.Ex, 
                                                self.Ey, 
                                                self.Ez, 
                                                self.diffxEy, 
                                                self.diffxEz, 
                                                self.diffyEx, 
                                                self.diffyEz, 
                                                self.diffzEx, 
                                                self.diffzEy
                                                )
            """

        #-----------------------------------------------------------#
        #--------------- Cast basic update equations ---------------#
        #-----------------------------------------------------------#

        CHx1 = (2.*self.mu_Hx[:,:-1,:-1] - self.mcon_Hx[:,:-1,:-1]*self.dt) / \
               (2.*self.mu_Hx[:,:-1,:-1] + self.mcon_Hx[:,:-1,:-1]*self.dt)
        CHx2 = (-2*self.dt) / (2.*self.mu_Hx[:,:-1,:-1] + self.mcon_Hx[:,:-1,:-1]*self.dt)

        CHy1 = (2.*self.mu_Hy[:-1,:,:-1] - self.mcon_Hy[:-1,:,:-1]*self.dt) / \
               (2.*self.mu_Hy[:-1,:,:-1] + self.mcon_Hy[:-1,:,:-1]*self.dt)
        CHy2 = (-2*self.dt) / (2.*self.mu_Hy[:-1,:,:-1] + self.mcon_Hy[:-1,:,:-1]*self.dt)

        CHz1 = (2.*self.mu_Hz[:-1,:-1,:] - self.mcon_Hz[:-1,:-1,:]*self.dt) / \
               (2.*self.mu_Hz[:-1,:-1,:] + self.mcon_Hz[:-1,:-1,:]*self.dt)
        CHz2 = (-2*self.dt) / (2.*self.mu_Hz[:-1,:-1,:] + self.mcon_Hz[:-1,:-1,:]*self.dt)

        self.Hx[:,:-1,:-1] = CHx1*self.Hx[:,:-1,:-1] + CHx2*(self.diffyEz[:,:-1,:-1]-self.diffzEy[:,:-1,:-1])
        self.Hy[:-1,:,:-1] = CHy1*self.Hy[:-1,:,:-1] + CHy2*(self.diffzEx[:-1,:,:-1]-self.diffxEz[:-1,:,:-1])
        self.Hz[:-1,:-1,:] = CHz1*self.Hz[:-1,:-1,:] + CHz2*(self.diffxEy[:-1,:-1,:]-self.diffyEx[:-1,:-1,:])

        if self.MPIrank >= 0 and self.MPIrank < (self.MPIsize-1):

	        # Update Hy and Hz at x=myNx-1
            self.Hy[myNx-1,:,:-1] = CHy1*self.Hy[myNx-1,:,:-1] + \
                                    CHy2*(self.diffzEx[myNx-1,:,:-1]-self.diffxEz[myNx-1,:,:-1])
            self.Hz[myNx-1,:-1,:] = CHz1*self.Hz[myNx-1,:-1,:] + \
                                    CHz2*(self.diffxEy[myNx-1,:-1,:]-self.diffyEx[myNx-1,:-1,:])
            """
            self.clib_core.updateH_rankFM   (\
                                                self.myNx, self.Ny, self.Nz,\
                                                self.dt, \
                                                self.mu_Hx, self.mu_Hy, self.mu_Hz, \
                                                self.mcon_Hx, self.mcon_Hy, self.mcon_Hz, \
                                                self.Hx, 
                                                self.Hy, 
                                                self.Hz, 
                                                self.diffxEy, 
                                                self.diffxEz, 
                                                self.diffyEx, 
                                                self.diffyEz, 
                                                self.diffzEx, 
                                                self.diffzEy
                                            )
            """

        elif self.MPIrank == (self.MPIsize-1): pass

            """
            self.clib_core.updateH_rank_L   (\
                                                self.myNx, self.Ny, self.Nz,\
                                                self.dt, \
                                                self.mu_Hx, self.mu_Hy, self.mu_Hz, \
                                                self.mcon_Hx, self.mcon_Hy, self.mcon_Hz, \
                                                self.Hx, 
                                                self.Hy, 
                                                self.Hz, 
                                                self.diffxEy, 
                                                self.diffxEz, 
                                                self.diffyEx, 
                                                self.diffyEz, 
                                                self.diffzEx, 
                                                self.diffzEy
                                            )
            """

        #-----------------------------------------------------------#
        #---------------- Apply PML when it is given ---------------#
        #-----------------------------------------------------------#

        # First rank
        if self.MPIrank == 0:
            if 'x' in self.PMLregion.keys():
                if '+' in self.PMLregion.get('x') and self.MPIsize == 1: self._PML_updateH_px()
                if '-' in self.PMLregion.get('x'): self._PML_updateH_mx()
            if 'y' in self.PMLregion.keys():
                if '+' in self.PMLregion.get('y'): self._PML_updateH_py()
                if '-' in self.PMLregion.get('y'): self._PML_updateH_my()
            if 'z' in self.PMLregion.keys():
                if '+' in self.PMLregion.get('z'): self._PML_updateH_pz()
                if '-' in self.PMLregion.get('z'): self._PML_updateH_mz()

        # Middle rank
        elif self.MPIrank > 0 and self.MPIrank < (self.MPIsize-1):
            if 'x' in self.PMLregion.keys():
                if '+' in self.PMLregion.get('x'): pass
                if '-' in self.PMLregion.get('x'): pass

            if 'y' in self.PMLregion.keys():

                if '+' in self.PMLregion.get('y'):

                    self.clib_PML.PML_updateH_py( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappay, self.PMLby, self.PMLay, \
                                                    self.mu_Hx, self.mu_Hz, \
                                                    self.mcon_Hx, self.mcon_Hz, \
                                                    self.Hx, 
                                                    self.Hz, 
                                                    self.diffyEx, 
                                                    self.diffyEz, 
                                                    self.psi_hxy_p, 
                                                    self.psi_hzy_p
                                                )

                if '-' in self.PMLregion.get('y'):

                    self.clib_PML.PML_updateH_my( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappay, self.PMLby, self.PMLay, \
                                                    self.mu_Hx, self.mu_Hz, \
                                                    self.mcon_Hx, self.mcon_Hz, \
                                                    self.Hx, 
                                                    self.Hz, 
                                                    self.diffyEx, 
                                                    self.diffyEz, 
                                                    self.psi_hxy_m, 
                                                    self.psi_hzy_m
                                                )

            if 'z' in self.PMLregion.keys():

                if '+' in self.PMLregion.get('z'):

                    self.clib_PML.PML_updateH_pz( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappaz, self.PMLbz, self.PMLaz, \
                                                    self.mu_Hx, self.mu_Hy, \
                                                    self.mcon_Hx, self.mcon_Hy, \
                                                    self.Hx, 
                                                    self.Hy, 
                                                    self.diffzEx, 
                                                    self.diffzEy, 
                                                    self.psi_hxz_p, 
                                                    self.psi_hyz_p
                                                )

                if '-' in self.PMLregion.get('z'):

                    self.clib_PML.PML_updateH_mz( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappaz, self.PMLbz, self.PMLaz, \
                                                    self.mu_Hx, self.mu_Hy, \
                                                    self.mcon_Hx, self.mcon_Hy, \
                                                    self.Hx, 
                                                    self.Hy, 
                                                    self.diffzEx, 
                                                    self.diffzEy, 
                                                    self.psi_hxz_m, 
                                                    self.psi_hyz_m
                                                )

        # Last rank
        elif self.MPIrank == (self.MPIsize-1) and self.MPIsize != 1:

            if 'x' in self.PMLregion.keys():

                if '+' in self.PMLregion.get('x'):

                    self.clib_PML.PML_updateH_px( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappax, self.PMLbx, self.PMLax, \
                                                    self.mu_Hy, self.mu_Hz, \
                                                    self.mcon_Hy, self.mcon_Hz, \
                                                    self.Hy, 
                                                    self.Hz, 
                                                    self.diffxEy, 
                                                    self.diffxEz, 
                                                    self.psi_hyx_p, 
                                                    self.psi_hzx_p
                                                )

                if '-' in self.PMLregion.get('x'): pass

            if 'y' in self.PMLregion.keys():

                if '+' in self.PMLregion.get('y'):

                    self.clib_PML.PML_updateH_py( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappay, self.PMLby, self.PMLay, \
                                                    self.mu_Hx, self.mu_Hz, \
                                                    self.mcon_Hx, self.mcon_Hz, \
                                                    self.Hx, 
                                                    self.Hz, 
                                                    self.diffyEx, 
                                                    self.diffyEz, 
                                                    self.psi_hxy_p, 
                                                    self.psi_hzy_p
                                                )

                if '-' in self.PMLregion.get('y'):

                    self.clib_PML.PML_updateH_my( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappay, self.PMLby, self.PMLay, \
                                                    self.mu_Hx, self.mu_Hz, \
                                                    self.mcon_Hx, self.mcon_Hz, \
                                                    self.Hx, 
                                                    self.Hz, 
                                                    self.diffyEx, 
                                                    self.diffyEz, 
                                                    self.psi_hxy_m, 
                                                    self.psi_hzy_m
                                                )

            if 'z' in self.PMLregion.keys():

                if '+' in self.PMLregion.get('z'):

                    self.clib_PML.PML_updateH_pz( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappaz, self.PMLbz, self.PMLaz, \
                                                    self.mu_Hx, self.mu_Hy, \
                                                    self.mcon_Hx, self.mcon_Hy, \
                                                    self.Hx, 
                                                    self.Hy, 
                                                    self.diffzEx, 
                                                    self.diffzEy, 
                                                    self.psi_hxz_p, 
                                                    self.psi_hyz_p
                                                )

                if '-' in self.PMLregion.get('z'):

                    self.clib_PML.PML_updateH_mz( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappaz, self.PMLbz, self.PMLaz, \
                                                    self.mu_Hx, self.mu_Hy, \
                                                    self.mcon_Hx, self.mcon_Hy, \
                                                    self.Hx, 
                                                    self.Hy, 
                                                    self.diffzEx, 
                                                    self.diffzEy, 
                                                    self.psi_hxz_m, 
                                                    self.psi_hyz_m
                                                )

        #-----------------------------------------------------------#
        #------------ Apply PBC along y when it is given -----------#
        #-----------------------------------------------------------#

        if self.myPBCregion_y == True:

            # Ranks except the last rank.
            if self.MPIsize == 0 or self.MPIrank < (self.MPIsize-1):

                self.clib_PBC.py_rankFM (\
                                            self.myNx, self.Ny, self.Nz, \
                                            self.dt, self.dx, self.dy, self.dz, \
                                            self.mu_Hx, self.mu_Hz, \
                                            self.mcon_Hx, self.mcon_Hz, \
                                            recvEylast, 
                                            self.Hx, 
                                            self.Hz, 
                                            self.Ex, 
                                            self.Ey, 
                                            self.Ez, 
                                            self.diffxEy, 
                                            self.diffyEx, 
                                            self.diffyEz, 
                                            self.diffzEy
                                        )

                # The first rank apply PBC on PML region.
                if self.MPIrank == 0 and '-' in self.PMLregion.get('x'):

                    self.clib_PBC.mxPML_pyPBC   (\
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt,\
                                                    self.PMLkappax, self.PMLbx, self.PMLax,\
                                                    self.mu_Hz, self.mcon_Hz,\
                                                    self.Hz, 
                                                    self.diffxEy, 
                                                    self.psi_hzx_m
                                                )

            # The last rank.
            elif self.MPIrank == (self.MPIsize-1):
                self.clib_PBC.py_rank_L (\
                                            self.myNx, self.Ny, self.Nz, \
                                            self.dt, self.dx, self.dy, self.dz, \
                                            self.mu_Hx, self.mu_Hz, \
                                            self.mcon_Hx, self.mcon_Hz, \
                                            self.Hx, 
                                            self.Hz, 
                                            self.Ex, 
                                            self.Ey, 
                                            self.Ez, 
                                            self.diffxEy, 
                                            self.diffyEx, 
                                            self.diffyEz, 
                                            self.diffzEy
                                        )

                # The last rank apply PBC on PML region.
                if '-' in self.PMLregion.get('x'):

                    self.clib_PBC.pxPML_pyPBC   (\
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt,\
                                                    self.PMLkappax, self.PMLbx, self.PMLax,\
                                                    self.mu_Hz, self.mcon_Hz,\
                                                    self.Hz, 
                                                    self.diffxEy, 
                                                    self.psi_hzx_p
                                                )


        else: pass

        #-----------------------------------------------------------#
        #------------ Apply PBC along z when it is given -----------#
        #-----------------------------------------------------------#

        if self.myPBCregion_z == True:

            # Ranks except the last rank.
            if self.MPIsize == 0 or self.MPIrank < (self.MPIsize-1):
                self.clib_PBC.pz_rankFM (\
                                            self.myNx, self.Ny, self.Nz, \
                                            self.dt, self.dx, self.dy, self.dz, \
                                            self.mu_Hx, self.mu_Hy, \
                                            self.mcon_Hx, self.mcon_Hy, \
                                            recvEzlast, 
                                            self.Hx, 
                                            self.Hy, 
                                            self.Ex, 
                                            self.Ey, 
                                            self.Ez, 
                                            self.diffxEz, 
                                            self.diffyEz, 
                                            self.diffzEx, 
                                            self.diffzEy
                                        )

                # The first rank apply PBC on PML region.
                if self.MPIrank == 0 and '-' in self.PMLregion.get('x'):

                    self.clib_PBC.mxPML_pzPBC   (\
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt,\
                                                    self.PMLkappax, self.PMLbx, self.PMLax,\
                                                    self.mu_Hy, self.mcon_Hy,\
                                                    self.Hy, 
                                                    self.diffxEz, 
                                                    self.psi_hyx_m
                                                )

            # The last rank.
            else:
                self.clib_PBC.pz_rank_L (\
                                            self.myNx, self.Ny, self.Nz, \
                                            self.dt, self.dx, self.dy, self.dz, \
                                            self.mu_Hx, self.mu_Hy, \
                                            self.mcon_Hx, self.mcon_Hy, \
                                            self.Hx, 
                                            self.Hy, 
                                            self.Ex, 
                                            self.Ey, 
                                            self.Ez, 
                                            self.diffxEz, 
                                            self.diffyEz, 
                                            self.diffzEx, 
                                            self.diffzEy
                                        )

                # The last rank apply PBC on PML region.
                if '-' in self.PMLregion.get('x'):

                    self.clib_PBC.pxPML_pzPBC   (\
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt,\
                                                    self.PMLkappax, self.PMLbx, self.PMLax,\
                                                    self.mu_Hy, self.mcon_Hy,\
                                                    self.Hy, 
                                                    self.diffxEz, 
                                                    self.psi_hyx_p
                                                )

        else: pass


    def updateE(self, tstep):
        """Update E field.

        Update E field for a given time step using various update equations.
        Basic update equations, PBC update equations and PML update equations are included here.

        Args:
            tstep : int
            Given time step to update E field

        Returns:
            None

        Raises:
            Error
        """

        #---------------------------------------------------------#
        #------------ MPI send Hy and Hz to next rank ------------#
        #---------------------------------------------------------#

        if self.MPIrank > (-1) and self.MPIrank < (self.MPIsize-1):

            if self.engine == 'cupy':
                sendEyfirst = cp.asnumpy(self.Hy[-1,:,:])
                sendEzfirst = cp.asnumpy(self.Hz[-1,:,:])

            else: # engine is numpy
                sendHylast = self.Hy[-1,:,:].copy()
                sendHzlast = self.Hz[-1,:,:].copy()

            self.MPIcomm.send(sendHylast, dest=(self.MPIrank+1), tag=(tstep*100+3))
            self.MPIcomm.send(sendHzlast, dest=(self.MPIrank+1), tag=(tstep*100+5))
        
        else: pass

        #---------------------------------------------------------#
        #--------- MPI recv Hy and Hz from previous rank ---------#
        #---------------------------------------------------------#

        if self.MPIrank > 0 and self.MPIrank < self.MPIsize:

            recvHyfirst = self.MPIcomm.recv( source=(self.MPIrank-1), tag=(tstep*100+3))
            recvHzfirst = self.MPIcomm.recv( source=(self.MPIrank-1), tag=(tstep*100+5))
        
            if self.engine == 'cupy':
                recvHyfirst = cp.asarray(recvHyfirst)
                recvHzfirst = cp.asarray(recvHzfirst)

        else: pass

        #-----------------------------------------------------------#
        #---------------------- Get derivatives --------------------#
        #-----------------------------------------------------------#

        if self.engine == 'cupy'
            iky = xp.expand_dims(1j*self.ky, 1)
            ikz = xp.expand_dims(1J*self.kz, 2)
            yshifter = xp.expand_dims(xp.exp(1j*self.ky*self.dy/2), 1)
            zshifter = xp.expand_dims(xp.exp(1j*self.kz*self.dz/2), 2)
        else:
            nax = np.newaxis
            iky = 1j*self.ky[:,nax,:]
            ikz = 1j*self.kz[:,:,nax]
            yshifter = xp.exp(1j*self.ky*self.dy/2)[:,nax,:]
            zshifter = xp.exp(1j*self.kz*self.dz/2)[:,:,nax]

	    # Get derivatives of Hy and Hz to update Ex
        self.diffyHz = xp.fft.ifftn(iky*xp.fft.fftn(self.Hz, axes=(1,)), axes=(1,))
        self.diffzHy = xp.fft.ifftn(ikz*xp.fft.fftn(self.Hy, axes=(2,)), axes=(2,))

	    # Get derivatives of Hx and Hz to update Ex
        self.diffzHx = xp.fft.ifftn(ikz*yshifter*xp.fft.fftn(self.Hx, axes=(1,2)), axes=(1,2))
        self.diffxHz[1:,:,1:] = (self.Hz[1:,:,1:] - self.Hz[:-1,:,1:]) / self.dz

	    # Get derivatives of Hx and Hy to update Ex
        self.diffxHy[1:,1:,:] = (self.Hy[1:,1:,:] - self.Hy[:-1,1:,:]) / self.dx
        self.diffyHx = xp.fft.ifftn(iky*zshifter*xp.fft.fftn(self.Hx, axes=(1,2)), axes=(1,2))

        if self.MPIrank == 0: pass

            """
            self.clib_core.get_diff_of_H_rank_F(\
                                                self.myNx, self.Ny, self.Nz,\
                                                self.dt, self.dx, self.dy, self.dz, \
                                                self.Hx, 
                                                self.Hy, 
                                                self.Hz, 
                                                self.diffxHy, 
                                                self.diffxHz, 
                                                self.diffyHx, 
                                                self.diffyHz, 
                                                self.diffzHx, 
                                                self.diffzHy
                                                )
            """
        else:

            # Get derivatives of Hx and Hz to update Ey at x=0
            self.diffxHz[0,:,1:] = (self.Hz[0,:,1:]-recvHzfirst[:,1:]) / self.dx
            self.diffxHy[0,1:,:] = (self.Hy[0,1:,:]-recvHyfirst[1:,:]) / self.dx
            """
            self.clib_core.get_diff_of_H_rankML(\
                                                self.myNx, self.Ny, self.Nz,\
                                                self.dt, self.dx, self.dy, self.dz, \
                                                recvHyfirst, 
                                                recvHzfirst, 
                                                self.Hx, 
                                                self.Hy, 
                                                self.Hz, 
                                                self.diffxHy, 
                                                self.diffxHz, 
                                                self.diffyHx, 
                                                self.diffyHz, 
                                                self.diffzHx, 
                                                self.diffzHy
                                                )
            """

        #-----------------------------------------------------------#
        #--------------- Cast basic update equations ---------------#
        #-----------------------------------------------------------#

        CEx1 = (2.*self.eps_Ex[:,1:,1:]-self.econ_Ex[:,1:,1:]*self.dt) \
               (2.*self.eps_Ex[:,1:,1:]+self.econ_Ex[:,1:,1:]*self.dt)
        CEx2 = (2.*self.dt) / (2.*self.eps_Ex[:,1:,1:]+self.econ_Ex[:,1:,1:]*self.dt)

        CEy1 = (2.*self.eps_Ey[1:,:,1:]-self.econ_Ey[1:,:,1:]*self.dt) \
               (2.*self.eps_Ey[1:,:,1:]+self.econ_Ey[1:,:,1:]*self.dt)
        CEy2 = (2.*self.dt) / (2.*self.eps_Ey[1:,:,1:]+self.econ_Ey[1:,:,1:]*self.dt)

        CEz1 = (2.*self.eps_Ez[1:,1:,:]-self.econ_Ez[1:,1:,:]*self.dt) \
               (2.*self.eps_Ez[1:,1:,:]+self.econ_Ez[1:,1:,:]*self.dt)
        CEz2 = (2.*self.dt) / (2.*self.eps_Ez[1:,1:,:]+self.econ_Ez[1:,1:,:]*self.dt)

        # PEC condition.
        CEx1[self.eps_Ex > 1e3] = 0.
        CEx2[self.eps_Ex > 1e3] = 0.
        CEy1[self.eps_Ey > 1e3] = 0.
        CEy2[self.eps_Ey > 1e3] = 0.
        CEz1[self.eps_Ez > 1e3] = 0.
        CEz2[self.eps_Ez > 1e3] = 0.

        # Update Ex, Ey, Ez
        self.Ex[:,1:,1:] = CEx1 * self.Ex[:,1:,1:] +CEx2 * (self.diffyHz[:,1:,1:] - self.diffzHy[:,1:,1:])
        self.Ey[1:,:,1:] = CEy1 * self.Ey[1:,:,1:] +CEy2 * (self.diffzHx[1:,:,1:] - self.diffxHz[1:,:,1:])
        self.Ez[1:,1:,:] = CEz1 * self.Ez[1:,1:,:] +CEz2 * (self.diffxHy[1:,1:,:] - self.diffyHx[1:,1:,:])

        if self.MPIrank == 0: pass

            """
            self.clib_core.updateE_rank_F   (\
                                                self.myNx, self.Ny, self.Nz,\
                                                self.dt, \
                                                self.eps_Ex, self.eps_Ey, self.eps_Ez, \
                                                self.econ_Ex, self.econ_Ey, self.econ_Ez, \
                                                self.Ex, 
                                                self.Ey, 
                                                self.Ez, 
                                                self.diffxHy, 
                                                self.diffxHz, 
                                                self.diffyHx, 
                                                self.diffyHz, 
                                                self.diffzHx, 
                                                self.diffzHy
                                            )
            """

        else:

            # Update Ey and Ez at x=0.
            self.Ey[0,:,1:] = CEy1 * self.Ey[0,:,1:] + CEy2 * (self.diffzHx[0,:,1:]-self.diffxHz[0,:,1:])
            self.Ez[0,1:,:] = CEz1 * self.Ez[0,1:,:] + CEz2 * (self.diffxHy[0,1:,:]-self.diffyHx[0,1:,:])

            """
            self.clib_core.updateE_rankML   (\
                                                self.myNx, self.Ny, self.Nz,\
                                                self.dt, \
                                                self.eps_Ex, self.eps_Ey, self.eps_Ez, \
                                                self.econ_Ex, self.econ_Ey, self.econ_Ez, \
                                                self.Ex, 
                                                self.Ey, 
                                                self.Ez, 
                                                self.diffxHy, 
                                                self.diffxHz, 
                                                self.diffyHx, 
                                                self.diffyHz, 
                                                self.diffzHx, 
                                                self.diffzHy
                                            )
            """

        #-----------------------------------------------------------#
        #---------------- Apply PML when it is given ---------------#
        #-----------------------------------------------------------#

        # First rank
        if self.MPIrank == 0:
            if 'x' in self.PMLregion.keys():
                if '+' in self.PMLregion.get('x') and self.MPIsize == 1:

                    self.clib_PML.PML_updateE_px( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappax, self.PMLbx, self.PMLax, \
                                                    self.eps_Ey, self.eps_Ez, \
                                                    self.econ_Ey, self.econ_Ez, \
                                                    self.Ey, 
                                                    self.Ez, 
                                                    self.diffxHy, 
                                                    self.diffxHz, 
                                                    self.psi_eyx_p, 
                                                    self.psi_ezx_p
                                                )

                if '-' in self.PMLregion.get('x'):

                    self.clib_PML.PML_updateE_mx( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappax, self.PMLbx, self.PMLax, \
                                                    self.eps_Ey, self.eps_Ez, \
                                                    self.econ_Ey, self.econ_Ez, \
                                                    self.Ey, 
                                                    self.Ez, 
                                                    self.diffxHy, 
                                                    self.diffxHz, 
                                                    self.psi_eyx_m,
                                                    self.psi_ezx_m
                                                )

            if 'y' in self.PMLregion.keys():

                if '+' in self.PMLregion.get('y'):

                    self.clib_PML.PML_updateE_py( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappay, self.PMLby, self.PMLay, \
                                                    self.eps_Ex, self.eps_Ez, \
                                                    self.econ_Ex, self.econ_Ez, \
                                                    self.Ex, 
                                                    self.Ez, 
                                                    self.diffyHx, 
                                                    self.diffyHz, 
                                                    self.psi_exy_p, 
                                                    self.psi_ezy_p
                                                )

                if '-' in self.PMLregion.get('y'):

                    self.clib_PML.PML_updateE_my( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappay, self.PMLby, self.PMLay, \
                                                    self.eps_Ex, self.eps_Ez, \
                                                    self.econ_Ex, self.econ_Ez, \
                                                    self.Ex, 
                                                    self.Ez, 
                                                    self.diffyHx, 
                                                    self.diffyHz, 
                                                    self.psi_exy_m, 
                                                    self.psi_ezy_m
                                                )

            if 'z' in self.PMLregion.keys():
                if '+' in self.PMLregion.get('z'):
                    self.clib_PML.PML_updateE_pz( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappaz, self.PMLbz, self.PMLaz, \
                                                    self.eps_Ex, self.eps_Ey, \
                                                    self.econ_Ex, self.econ_Ey, \
                                                    self.Ex, 
                                                    self.Ey, 
                                                    self.diffzHx, 
                                                    self.diffzHy, 
                                                    self.psi_exz_p, 
                                                    self.psi_eyz_p
                                                )

                if '-' in self.PMLregion.get('z'):
                    self.clib_PML.PML_updateE_mz( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappaz, self.PMLbz, self.PMLaz, \
                                                    self.eps_Ex, self.eps_Ey, \
                                                    self.econ_Ex, self.econ_Ey, \
                                                    self.Ex, 
                                                    self.Ey, 
                                                    self.diffzHx, 
                                                    self.diffzHy, 
                                                    self.psi_exz_m, 
                                                    self.psi_eyz_m
                                                )

        # Middle rank
        elif self.MPIrank > 0 and self.MPIrank < (self.MPIsize-1):

            if 'x' in self.PMLregion.keys():
                if '+' in self.PMLregion.get('x'): pass
                if '-' in self.PMLregion.get('x'): pass

            if 'y' in self.PMLregion.keys():

                if '+' in self.PMLregion.get('y'):

                    self.clib_PML.PML_updateE_py( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappay, self.PMLby, self.PMLay, \
                                                    self.eps_Ex, self.eps_Ez, \
                                                    self.econ_Ex, self.econ_Ez, \
                                                    self.Ex, 
                                                    self.Ez, 
                                                    self.diffyHx, 
                                                    self.diffyHz, 
                                                    self.psi_exy_p, 
                                                    self.psi_ezy_p
                                                )

                if '-' in self.PMLregion.get('y'):

                    self.clib_PML.PML_updateE_my( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappay, self.PMLby, self.PMLay, \
                                                    self.eps_Ex, self.eps_Ez, \
                                                    self.econ_Ex, self.econ_Ez, \
                                                    self.Ex, 
                                                    self.Ez, 
                                                    self.diffyHx, 
                                                    self.diffyHz, 
                                                    self.psi_exy_m, 
                                                    self.psi_ezy_m
                                                )

            if 'z' in self.PMLregion.keys():
                if '+' in self.PMLregion.get('z'):
                    self.clib_PML.PML_updateE_pz( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappaz, self.PMLbz, self.PMLaz, \
                                                    self.eps_Ex, self.eps_Ey, \
                                                    self.econ_Ex, self.econ_Ey, \
                                                    self.Ex, 
                                                    self.Ey, 
                                                    self.diffzHx, 
                                                    self.diffzHy, 
                                                    self.psi_exz_p, 
                                                    self.psi_eyz_p
                                                )

                if '-' in self.PMLregion.get('z'):
                    self.clib_PML.PML_updateE_mz( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappaz, self.PMLbz, self.PMLaz, \
                                                    self.eps_Ex, self.eps_Ey, \
                                                    self.econ_Ex, self.econ_Ey, \
                                                    self.Ex, 
                                                    self.Ey, 
                                                    self.diffzHx, 
                                                    self.diffzHy, 
                                                    self.psi_exz_m, 
                                                    self.psi_eyz_m
                                                )

        # Last rank
        elif self.MPIrank == (self.MPIsize-1) and self.MPIsize != 1:
            if 'x' in self.PMLregion.keys():
                if '+' in self.PMLregion.get('x'):

                    self.clib_PML.PML_updateE_px( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappax, self.PMLbx, self.PMLax, \
                                                    self.eps_Ey, self.eps_Ez, \
                                                    self.econ_Ey, self.econ_Ez, \
                                                    self.Ey, 
                                                    self.Ez, 
                                                    self.diffxHy, 
                                                    self.diffxHz, 
                                                    self.psi_eyx_p, 
                                                    self.psi_ezx_p
                                                )

                if '-' in self.PMLregion.get('x'): pass

            if 'y' in self.PMLregion.keys():

                if '+' in self.PMLregion.get('y'):

                    self.clib_PML.PML_updateE_py( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappay, self.PMLby, self.PMLay, \
                                                    self.eps_Ex, self.eps_Ez, \
                                                    self.econ_Ex, self.econ_Ez, \
                                                    self.Ex, 
                                                    self.Ez, 
                                                    self.diffyHx, 
                                                    self.diffyHz, 
                                                    self.psi_exy_p,
                                                    self.psi_ezy_p
                                                )

                if '-' in self.PMLregion.get('y'):

                    self.clib_PML.PML_updateE_my( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappay, self.PMLby, self.PMLay, \
                                                    self.eps_Ex, self.eps_Ez, \
                                                    self.econ_Ex, self.econ_Ez, \
                                                    self.Ex, 
                                                    self.Ez, 
                                                    self.diffyHx, 
                                                    self.diffyHz,
                                                    self.psi_exy_m,
                                                    self.psi_ezy_m
                                                )

            if 'z' in self.PMLregion.keys():
                if '+' in self.PMLregion.get('z'): 
                    self.clib_PML.PML_updateE_pz( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappaz, self.PMLbz, self.PMLaz, \
                                                    self.eps_Ex, self.eps_Ey, \
                                                    self.econ_Ex, self.econ_Ey, \
                                                    self.Ex,
                                                    self.Ey,
                                                    self.diffzHx,
                                                    self.diffzHy,
                                                    self.psi_exz_p,
                                                    self.psi_eyz_p
                                                )

                if '-' in self.PMLregion.get('z'):
                    self.clib_PML.PML_updateE_mz( \
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt, \
                                                    self.PMLkappaz, self.PMLbz, self.PMLaz, \
                                                    self.eps_Ex, self.eps_Ey, \
                                                    self.econ_Ex, self.econ_Ey, \
                                                    self.Ex,
                                                    self.Ey,
                                                    self.diffzHx,
                                                    self.diffzHy,
                                                    self.psi_exz_m,
                                                    self.psi_eyz_m
                                                )

        #-----------------------------------------------------------#
        #------------ Apply PBC along y when it is given -----------#
        #-----------------------------------------------------------#

        if self.myPBCregion_y == True:

            # The first rank.
            if self.MPIrank == 0 :

                self.clib_PBC.my_rank_F( \
                                            self.myNx, self.Ny, self.Nz, \
                                            self.dt, self.dx, self.dy, self.dz,\
                                            self.eps_Ex, self.eps_Ez, \
                                            self.econ_Ex, self.econ_Ez, \
                                            self.Ex, 
                                            self.Ez, 
                                            self.Hx, 
                                            self.Hy, 
                                            self.Hz, 
                                            self.diffxHy, 
                                            self.diffyHx, 
                                            self.diffyHz, 
                                            self.diffzHy
                                        )

                # The first rank apply PBC on PML region.
                if '-' in self.PMLregion.get('x'):

                    self.clib_PBC.mxPML_myPBC   (\
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt,\
                                                    self.PMLkappax, self.PMLbx, self.PMLax,\
                                                    self.eps_Ez, self.econ_Ez,\
                                                    self.Ez, 
                                                    self.diffxHy, 
                                                    self.psi_ezx_m
                                                )

            # Ranks except the first rank.
            else:   
                self.clib_PBC.my_rankML( \
                                            self.myNx, self.Ny, self.Nz, \
                                            self.dt, self.dx, self.dy, self.dz,\
                                            self.eps_Ex, self.eps_Ez, \
                                            self.econ_Ex, self.econ_Ez, \
                                            recvHyfirst,
                                            self.Ex,
                                            self.Ez, 
                                            self.Hx, 
                                            self.Hy, 
                                            self.Hz,
                                            self.diffxHy, 
                                            self.diffyHx, 
                                            self.diffyHz, 
                                            self.diffzHy
                                        )

                # The last rank apply PBC on PML region.
                if self.MPIrank == (self.MPIsize-1) and '-' in self.PMLregion.get('x'):

                    self.clib_PBC.pxPML_myPBC   (\
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt,\
                                                    self.PMLkappax, self.PMLbx, self.PMLax,\
                                                    self.eps_Ez, self.econ_Ez,\
                                                    self.Ez, 
                                                    self.diffxHy,
                                                    self.psi_ezx_p
                                                )

        else: pass

        #-----------------------------------------------------------#
        #------------ Apply PBC along z when it is given -----------#
        #-----------------------------------------------------------#

        if self.myPBCregion_z == True:

            # The first rank.
            if self.MPIrank == 0:

                self.clib_PBC.mz_rank_F (\
                                            self.myNx, self.Ny, self.Nz, \
                                            self.dt, self.dx, self.dy, self.dz,\
                                            self.eps_Ex, self.eps_Ez, \
                                            self.econ_Ex, self.econ_Ez, \
                                            self.Ex, 
                                            self.Ey, 
                                            self.Hx, 
                                            self.Hy, 
                                            self.Hz, 
                                            self.diffxHz, 
                                            self.diffyHz, 
                                            self.diffzHx, 
                                            self.diffzHy
                                        )

                # The first rank applies PBC on PML region.
                if '-' in self.PMLregion.get('x'):

                    self.clib_PBC.mxPML_mzPBC   (\
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt,\
                                                    self.PMLkappax, self.PMLbx, self.PMLax,\
                                                    self.eps_Ey, self.econ_Ey,\
                                                    self.Ey, 
                                                    self.diffxHz, 
                                                    self.psi_eyx_m
                                                )

            # Ranks except the first rank.
            else:

                self.clib_PBC.mz_rankML (\
                                            self.myNx, self.Ny, self.Nz, \
                                            self.dt, self.dx, self.dy, self.dz,\
                                            self.eps_Ex, self.eps_Ez, \
                                            self.econ_Ex, self.econ_Ez, \
                                            recvHzfirst, 
                                            self.Ex, 
                                            self.Ey, 
                                            self.Hx, 
                                            self.Hy, 
                                            self.Hz, 
                                            self.diffxHz, 
                                            self.diffyHz, 
                                            self.diffzHx, 
                                            self.diffzHy
                                        )

                # The last rank applies PBC on PML region.
                if self.MPIrank == (self.MPIsize-1) and '-' in self.PMLregion.get('x'):

                    self.clib_PBC.pxPML_mzPBC   (\
                                                    self.myNx, self.Ny, self.Nz, self.npml,\
                                                    self.dt,\
                                                    self.PMLkappax, self.PMLbx, self.PMLax,\
                                                    self.eps_Ey, self.econ_Ey,\
                                                    self.Ey, 
                                                    self.diffxHz, 
                                                    self.psi_eyx_p
                                                )

        else: pass

    def _PML_updateH_px(self):

        odd = slice(1,-2,2)

        # Update Hy at x+.
        psiidx = [slice(0,-1), slice(0,None), slice(0,-1)]
        myidx = [slice(-self.npml, myNx-1), slice(0,None), slice(0,-1)]

        CHy2 = (-2*self.dt) / (2.*self.mu_Hy[myidx] + self.mcon_Hy[myidx]*self.dt)
        self.psi_hyx_p[psiidx] = (self.PMLbx[odd,None,None]*self.psi_hyx_p[psiidx]) + \
                                    (self.PMLax[odd,None,None]*self.diffxEz[myidx])
        self.Hy[myidx] += CHy2 * (-((1./self.PMLkappax[odd,None,None] - 1.) *\
                            self.diffxEz[myidx]) - self.psi_hyx_p[psiidx])

        # Update Hz at x+.
        psiidx = [slice(0,-1), slice(0,-1), slice(0,None)]
        myidx = [slice(-self.npml, myNx-1), slice(0,-1), slice(0,None)]

        CHz2 = (-2*self.dt) / (2.*self.mu_Hz[myidx] + self.mcon_Hz[myidx]*self.dt)
        self.psi_hzx_p[psiidx] = (self.PMLbx[odd,None,None]*self.psi_hzx_p[psiidx]) + \
                                    (self.PMLax[odd,None,None]*self.diffxEy[myidx])
        self.Hz[myidx] += CHz2 * (-((1./self.PMLkappax[odd,None,None] - 1.) *\
                            self.diffxEy[myidx]) - self.psi_hzx_p[psiidx])

        """
        self.clib_PML.PML_updateH_px( \
                                        self.myNx, self.Ny, self.Nz, self.npml,\
                                        self.dt, \
                                        self.PMLkappax, self.PMLbx, self.PMLax, \
                                        self.mu_Hy, self.mu_Hz, \
                                        self.mcon_Hy, self.mcon_Hz, \
                                        self.Hy, 
                                        self.Hz, 
                                        self.diffxEy, 
                                        self.diffxEz, 
                                        self.psi_hyx_p, 
                                        self.psi_hzx_p
                                    )
        """
    
    def _PML_updateH_mx(self):

        even = slice(-2,None,-2)

        # Update Hy at x-.
        psiidx = [slice(0,self.npml), slice(0,None), slice(0,-1)]
        myidx = [slice(0,self.npml), slice(0,None), slice(0,-1)]

        CHy2 = (-2*self.dt) / (2.*self.mu_Hy[myidx] + self.mcon_Hy[myidx]*self.dt)
        self.psi_hyx_m[psiidx] = (self.PMLbx[even,None,None]*self.psi_hyx_m[psiidx]) + \
                                    (self.PMLax[even,None,None]*self.diffxEz[myidx])
        self.Hy[myidx] += CHy2 * (-((1./self.PMLkappax[even,None,None] - 1.) *\
                            self.diffxEz[myidx]) - self.psi_hyx_m[psiidx])

        # Update Hz at x-.
        psiidx = [slice(0, self.npml), slice(0,-1), slice(0,None)]
        myidx = [slice(0, self.npml), slice(0,-1), slice(0,None)]

        CHz2 = (-2*self.dt) / (2.*self.mu_Hz[myidx] + self.mcon_Hz[myidx]*self.dt)
        self.psi_hzx_m[psiidx] = (self.PMLbx[even,None,None]*self.psi_hzx_m[psiidx]) + \
                                    (self.PMLax[even,None,None]*self.diffxEy[myidx])
        self.Hz[myidx] += CHz2 * (-((1./self.PMLkappax[even,None,None] - 1.) *\
                            self.diffxEy[myidx]) - self.psi_hzx_m[psiidx])

        """
        self.clib_PML.PML_updateH_mx( \
                                        self.myNx, self.Ny, self.Nz, self.npml,\
                                        self.dt, \
                                        self.PMLkappax, self.PMLbx, self.PMLax, \
                                        self.mu_Hy, self.mu_Hz, \
                                        self.mcon_Hy, self.mcon_Hz, \
                                        self.Hy, 
                                        self.Hz, 
                                        self.diffxEy, 
                                        self.diffxEz, 
                                        self.psi_hyx_m, 
                                        self.psi_hzx_m
                                    )
        """

    def _PML_updateH_py(self):

        odd = slice(1,None,2)
        psiidx = [slice(0,None), slice(0,self.npml), slice(0,None)]
        myidx = [slice(0,None), slice(-self.npml,None), slice(0,None)]

        # Update Hx at y+.
        CHx2 = (-2.*self.dt) / (2.*self.mu_Hx[myidx] + self.mcon_Hx[myidx]*self.dt)

        self.psi_hxy_p[psiidx] = (self.PMLby[None,odd,None]*self.psi_hxy_p[psiidx]) \
                                + (self.PMLay[None,odd,None]*self.diffyEz[myidx]
        self.Hx[myidx] += CHx2 * (+((1./self.PMLkappamaxy[None,odd,None] - 1.) * \
                            self.diffyEz[myidx])+self.psi_hxy_p[psiidx])

        # Update Hz at y+.
        CHz2 = (-2.*self.dt) / (2.*self.mu_Hz[myidx] + self.mcon_Hz[myidx]*self.dt)

        self.psi_hzy_p[psiidx] = (self.PMLby[None,odd,None] * self.psi_hzy_p[psiidx]) \
                                + (self.PMLay[None,odd,None] * self.diffyEx[myidx])
        self.Hz[myidx] += CHz2 * (-((1./self.PMLkappamaxy[None,odd,None]-1.) * \
                            self.diffyEx[myidx])-self.psi_hzy_p[psiidx])

        """
        self.clib_PML.PML_updateH_py( \
                                        self.myNx, self.Ny, self.Nz, self.npml,\
                                        self.dt, \
                                        self.PMLkappay, self.PMLby, self.PMLay, \
                                        self.mu_Hx, self.mu_Hz, \
                                        self.mcon_Hx, self.mcon_Hz, \
                                        self.Hx, 
                                        self.Hz, 
                                        self.diffyEx, 
                                        self.diffyEz, 
                                        self.psi_hxy_p, 
                                        self.psi_hzy_p
                                    )
        """

    def _PML_updateH_my(self):

        even = slice(-2,None,-2)
        psiidx = [slice(0,None), slice(0, self.npml), slice(0,None)]
        myidx = [slice(0,None), slice(0,self.npml), slice(0,None)]

        # Update Hx at y-.
        CHx2 =  (-2*self.dt) / (2.*self.mu_Hx[myidx] + self.mcon_Hx[myidx]*self.dt);

        self.psi_hxy_m[psiidx] = (self.PMLby[None,even,None] * self.psi_hxy_m[psiidx]) + \
                                (self.PMLay[None,even,None] * self.diffyEz[myidx]);
        self.Hx[myidx] += CHx2 * (+((1./self.PMLkappay[None,even,None] - 1.) * \
                            self.diffyEz[myidx]) + self.psi_hxy_m[psiidx]);

        # Update Hz at y-.
        CHz2 =  (-2*self.dt) / (2.*self.mu_Hz[myidx] + self.mcon_Hz[myidx]*self.dt);

        self.psi_hzy_m[psiidx] = (self.PMLby[None,even,None] * self.psi_hzy_m[psiidx]) + \
                                    (self.PMLay[None,even,None] * self.diffyEx[myidx]);
        self.Hz[myidx] += CHz2 * (-((1./self.PMLkappay[None,even,None] - 1.) * \
                            self.diffyEx[myidx]) - self.psi_hzy_m[psiidx]);
        """
        self.clib_PML.PML_updateH_my( \
                                        self.myNx, self.Ny, self.Nz, self.npml,\
                                        self.dt, \
                                        self.PMLkappay, self.PMLby, self.PMLay, \
                                        self.mu_Hx, self.mu_Hz, \
                                        self.mcon_Hx, self.mcon_Hz, \
                                        self.Hx, 
                                        self.Hz, 
                                        self.diffyEx, 
                                        self.diffyEz, 
                                        self.psi_hxy_m, 
                                        self.psi_hzy_m
                                    )
        """
    def _PML_updateH_pz(self):

        odd = slice(1,None,2)
        psiidx = [slice(0,None), slice(0,None), slice(0,self.npml)]
        myidx = [slice(0,None), slice(0,None), slice(-self.npml, None)]

        # Update Hx at z+.
        CHx2 =	(-2*self.dt) / (2.*self.mu_Hx[myidx] + self.mcon_Hx[myidx]*self.dt);
        
        self.psi_hxz_p[psiidx] = (self.PMLbz[None,None,odd] * self.psi_hxz_p[psiidx]) +\
                                    (self.PMLaz[None,None,odd] * self.diffzEy[myidx]);
        self.Hx[myidx] += CHx2 * (-((1./self.PMLkappaz[None,None,odd] - 1.) * \
                            self.diffzEy[myidx]) - self.psi_hxz_p[psiidx]);

        # Update Hy at z+.
        CHy2 =	(-2*self.dt) / (2.*self.mu_Hy[myidx] + self.mcon_Hy[myidx]*self.dt);
        
        self.psi_hyz_p[psiidx] = (self.PMLbz[None,None,odd] * self.psi_hyz_p[psiidx]) + \
                                    (self.PMLaz[None,None,odd] * self.diffzEx[myidx]);
        self.Hy[myidx] += CHy2 * (+((1./self.PMLkappaz[None,None,odd] - 1.) * \
                            self.diffzEx[myidx]) + self.psi_hyz_p[psiidx]);
        """
        self.clib_PML.PML_updateH_pz( \
                                        self.myNx, self.Ny, self.Nz, self.npml,\
                                        self.dt, \
                                        self.PMLkappaz, self.PMLbz, self.PMLaz, \
                                        self.mu_Hx, self.mu_Hy, \
                                        self.mcon_Hx, self.mcon_Hy, \
                                        self.Hx, 
                                        self.Hy, 
                                        self.diffzEx, 
                                        self.diffzEy, 
                                        self.psi_hxz_p, 
                                        self.psi_hyz_p
                                    )
        """
    def _PML_updateH_mz(self):
        even = slice(-2,None,-2)
        psiidx = [slice(0,None), slice(0,None), slice(0,self.npml)]
        myidx = [slice(0,None), slice(0,None), slice(0,self.npml)]

        # Update Hx at z-.
        CHx2 =	(-2*self.dt) / (2.*self.mu_Hx[myidx] + self.mcon_Hx[myidx]*self.dt);
        
        self.psi_hxz_m[psiidx] = (self.PMLbz[None,None,even] * self.psi_hxz_m[psiidx]) + \
                                    (self.PMLaz[None,None,even] * self.diffzEy[myidx]);
        self.Hx[myidx] += CHx2 * (-((1./self.PMLkappaz[None,None,even] - 1.) * \
                            self.diffzEy[myidx]) - self.psi_hxz_m_re[psiidx]);
        # Update Hy at z-.
        CHy2 =	(-2*self.dt) / (2.*self.mu_Hy[myidx] + self.mcon_Hy[myidx]*self.dt);
        
        self.psi_hyz_m[psiidx] = (self.PMLbz[None,None,even] * self.psi_hyz_m[psiidx]) + \
                                    (self.PMLaz[None,None,even] * self.diffzEx[myidx]);
        self.Hy[myidx] += CHy2 * (+((1./self.PMLkappaz[None,None,even] - 1.) * \
                            self.diffzEx[myidx]) + self.psi_hyz_m[psiidx]);
        """
        self.clib_PML.PML_updateH_mz( \
                                        self.myNx, self.Ny, self.Nz, self.npml,\
                                        self.dt, \
                                        self.PMLkappaz, self.PMLbz, self.PMLaz, \
                                        self.mu_Hx, self.mu_Hy, \
                                        self.mcon_Hx, self.mcon_Hy, \
                                        self.Hx, 
                                        self.Hy, 
                                        self.diffzEx, 
                                        self.diffzEy, 
                                        self.psi_hxz_m, 
                                        self.psi_hyz_m
                                    )
        """
    def _PML_updateE_px(self):
    def _PML_updateE_mx(self):
    def _PML_updateE_py(self):
    def _PML_updateE_my(self):
    def _PML_updateE_pz(self):
    def _PML_updateE_mz(self):

class Empty3D(object):
    
    def __init__(self, grid, gridgap, courant, dt, tsteps, dtype, **kwargs):
        """Create Simulation Space.

            ex) Space.grid((128,128,600), (50*nm,50*nm,5*nm), dtype=xp.float64)

        PARAMETERS
        ----------
        grid : tuple
            define the x,y,z grid.

        gridgap : tuple
            define the dx, dy, dz.

        dtype : class numpy dtype
            choose xp.float32 or xp.float64

        kwargs : string
            
            supported arguments
            -------------------

            courant : float
                Set the courant number. For FDTD, default is 1./2

        RETURNS
        -------
        None
        """

        self.nm = 1e-9
        self.um = 1e-6  

        self.dtype    = dtype
        self.MPIcomm  = MPI.COMM_WORLD
        self.MPIrank  = self.MPIcomm.Get_rank()
        self.MPIsize  = self.MPIcomm.Get_size()
        self.hostname = MPI.Get_processor_name()

        assert len(grid)    == 3, "Simulation grid should be a tuple with length 3."
        assert len(gridgap) == 3, "Argument 'gridgap' should be a tuple with length 3."

        self.tsteps = tsteps        

        self.grid = grid
        self.Nx   = self.grid[0]
        self.Ny   = self.grid[1]
        self.Nz   = self.grid[2]
        self.TOTAL_NUM_GRID = self.Nx * self.Ny * self.Nz
        self.TOTAL_NUM_GRID_SIZE = (self.dtype(1).nbytes * self.TOTAL_NUM_GRID) / 1024 / 1024
        
        self.Nxc = int(self.Nx / 2)
        self.Nyc = int(self.Ny / 2)
        self.Nzc = int(self.Nz / 2)
        
        self.gridgap = gridgap
        self.dx = self.gridgap[0]
        self.dy = self.gridgap[1]
        self.dz = self.gridgap[2]

        self.Lx = self.Nx * self.dx
        self.Ly = self.Ny * self.dy
        self.Lz = self.Nz * self.dz

        self.VOLUME = self.Lx * self.Ly * self.Lz

        if self.MPIrank == 0:
            print("VOLUME of the space: {:.2e}" .format(self.VOLUME))
            print("Number of grid points: {:5d} x {:5d} x {:5d}" .format(self.Nx, self.Ny, self.Nz))
            print("Grid spacing: {:.3f} nm, {:.3f} nm, {:.3f} nm" .format(self.dx/self.nm, self.dy/self.nm, self.dz/self.nm))

        self.MPIcomm.Barrier()

        self.courant = courant

        for key, value in kwargs.items():
            if key == 'courant': self.courant = value

        self.dt = dt
        self.maxdt = 1. / c / xp.sqrt( (1./self.dx)**2 + (1./self.dy)**2 + (1./self.dz)**2 )

        assert (c * self.dt * xp.sqrt( (1./self.dx)**2 + (1./self.dy)**2 + (1./self.dz)**2 )) < 1.

        """
        For more details about maximum dt in the Hybrid PSTD-FDTD method, see
        Combining the FDTD and PSTD methods, Y.F.Leung, C.H. Chan,
        Microwave and Optical technology letters, Vol.23, No.4, November 20 1999.
        """

        self.myPMLregion_x = None
        self.myPMLregion_y = None
        self.myPMLregion_z = None
        self.myPBCregion_x = False
        self.myPBCregion_y = False
        self.myPBCregion_z = False
        self.myBBCregion_x = False
        self.myBBCregion_y = False
        self.myBBCregion_z = False

        assert self.dt < self.maxdt, "Time interval is too big so that causality is broken. Lower the courant number."
        assert float(self.Nx) % self.MPIsize == 0., "Nx must be a multiple of the number of nodes."
        
        ############################################################################
        ################# Set the loc_grid each node should possess ################
        ############################################################################

        self.myNx     = int(self.Nx/self.MPIsize)
        self.loc_grid = (self.myNx, self.Ny, self.Nz)

        self.Ex = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.Ey = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.Ez = xp.zeros(self.loc_grid, dtype=self.dtype)

        self.Hx = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.Hy = xp.zeros(self.loc_grid, dtype=self.dtype)
        self.Hz = xp.zeros(self.loc_grid, dtype=self.dtype)

        ###############################################################################

        ###############################################################################
        ####################### Slices of zgrid that each node got ####################
        ###############################################################################
        
        self.myNx_slices = []
        self.myNx_indice = []

        for rank in range(self.MPIsize):

            xsrt = (rank  ) * self.myNx
            xend = (rank+1) * self.myNx

            self.myNx_slices.append(slice(xsrt, xend))
            self.myNx_indice.append(     (xsrt, xend))

        self.MPIcomm.Barrier()
        #print("rank {:>2}:\tmy xindex: {},\tmy xslice: {}" \
        #       .format(self.MPIrank, self.myNx_indice[self.MPIrank], self.myNx_slices[self.MPIrank]))

    def get_SF(self, TF, IF):
        """Get scattered field

        Paramters
        ---------
        TF: Basic3D class object.
            Total field.

        IF: Basic3D class object.
            Input field.

        Returns
        -------
        None
        """

        self.Ex = TF.Ex - IF.Ex
        self.Ey = TF.Ey - IF.Ey
        self.Ez = TF.Ez - IF.Ez

        self.Hx = TF.Hx - IF.Hx
        self.Hy = TF.Hy - IF.Hy
        self.Hz = TF.Hz - IF.Hz