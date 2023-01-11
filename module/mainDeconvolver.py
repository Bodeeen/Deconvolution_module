import os
import time
import numpy as np
import numba
from numba import cuda
import cupy as cp
from module.kernelGeneration import KernelHandler
from module.transformMatGeneration import TransformMatHandler
from module.gpuTransforms import convTransform, invConvTransform
from module.dataFiddler import DataFiddler
from module.DataIO_tools import DataIO_tools
import json

class Deconvolver:

    def __init__(self):

        self.DF = DataFiddler()
        self.KH = KernelHandler()
        self.tMatHandler = TransformMatHandler()

        self.mempool = cp.get_default_memory_pool()

    def setAndLoadData(self, path, dataPropertiesDict):
        self.DF.loadData(path, dataPropertiesDict)

    def Deconvolve(self, imFormationModelParameters, algOptionsDict, saveOptions):

        """Unpack options"""
        saveToDisc = saveOptions['Save to disc']
        if saveToDisc:
            try:
                saveMode = saveOptions['Save mode']
                if saveMode == 'Progression':
                    try:
                        progressionMode = saveOptions['Progression mode']
                    except KeyError:
                        print('Progression mode missing')
            except KeyError:
                print('Save mode missing')
            try:
                saveFolder = saveOptions['Save folder']
                saveName = saveOptions['Save name']
            except KeyError:
                print('Save folder and/or name missing')
        else:
            saveMode = None
            progressionMode = None

        iterations = algOptionsDict['Iterations']

        K = self.KH.makePLSRKernel(self.DF.getDataPropertiesDict(), imFormationModelParameters, algOptionsDict)
        M = self.tMatHandler.makeSOLSTransformMatrix(self.DF.getDataPropertiesDict(), algOptionsDict)

        data = self.DF.getPreprocessedData()
        assert self.checkData(data), "Something wrong with data"

        dataShape = np.shape(data)
        reconShape = np.ceil(np.matmul(M, dataShape)).astype(int)
        """Prepare GPU blocks"""
        threadsperblock = 8
        blocks_per_grid_z = (dataShape[0] + (threadsperblock - 1)) // threadsperblock
        blocks_per_grid_y = (dataShape[1] + (threadsperblock - 1)) // threadsperblock
        blocks_per_grid_x = (dataShape[2] + (threadsperblock - 1)) // threadsperblock

        """Prepare arrays"""
        dev_data = cp.array(data)
        dev_dataOnes = cp.ones_like(data, dtype=float)
        dev_Ht_of_ones = cp.zeros(reconShape, dtype=float)
        dev_K = cp.array(K)
        dev_M = cp.array(M)
        invConvTransform[
            (blocks_per_grid_z, blocks_per_grid_y, blocks_per_grid_x), (
                threadsperblock, threadsperblock, threadsperblock)](
            dev_dataOnes, dev_Ht_of_ones, dev_K, dev_M)
        del dev_dataOnes
        dev_Ht_of_ones = dev_Ht_of_ones.clip(
            0.3 * cp.max(dev_Ht_of_ones))  # Avoid divide by zero and crazy high guesses outside measured region, 0.3 is emperically chosen
        self.mempool.free_all_blocks()
        dev_currentReconstruction = cp.ones(reconShape, dtype=float)
        dev_dataCanvas = cp.zeros(dataShape, dtype=float)
        dev_sampleCanvas = cp.zeros(reconShape, dtype=float)

        """Prepare saving"""
        if saveMode == 'Progression':
            saveRecons = []
            if progressionMode == 'Logarithmic':
                indices = (iterations - np.arange(0, np.floor(np.log2(iterations)))**2)[::-1]
        for i in range(iterations):
            print('Iteration: ', i)
            """Zero arrays"""
            print('Made arrays')
            t1 = time.time()
            convTransform[
                (blocks_per_grid_z, blocks_per_grid_y, blocks_per_grid_x), (
                threadsperblock, threadsperblock, threadsperblock)](
                dev_dataCanvas, dev_currentReconstruction, dev_K, dev_M)
            cuda.synchronize()
            t2 = time.time()
            elapsed = t2-t1
            print('Calculated dfg, elapsed = ', elapsed)
            cp.divide(dev_data, dev_dataCanvas, dev_dataCanvas) #dataCanvas now stores the error
            print('Calculated error')
            t1 = time.time()
            invConvTransform[
                (blocks_per_grid_z, blocks_per_grid_y, blocks_per_grid_x), (
                threadsperblock, threadsperblock, threadsperblock)](
                dev_dataCanvas, dev_sampleCanvas, dev_K, dev_M) #Sample canvas now stores the distributed error
            cuda.synchronize()
            t2 = time.time()
            elapsed = t2-t1
            print('Distributed error, elapsed = ', elapsed)
            cp.divide(dev_sampleCanvas, dev_Ht_of_ones, out=dev_sampleCanvas) #Sample canvas now stores the "correction factor"
            cp.multiply(dev_currentReconstruction, dev_sampleCanvas, out=dev_currentReconstruction)
            if saveMode == 'Progression' and (progressionMode == 'All' or i in indices):
                saveRecons.append(cp.asnumpy(dev_currentReconstruction))

        finalReconstruction = cp.asnumpy(dev_currentReconstruction)
        del dev_currentReconstruction
        del dev_dataCanvas
        del dev_sampleCanvas
        self.mempool.free_all_blocks()

        if saveToDisc:
            if saveMode == 'Final':
                saveDataPath = os.path.join(saveFolder, saveName + '_FinalDeconvolved.tif')
                DataIO_tools.save_data(finalReconstruction, saveDataPath)
            elif saveMode == 'Progression':
                saveRecons = np.asarray(saveRecons)
                saveDataPath = os.path.join(saveFolder, saveName + '_DeconvolutionProgression.tif')
                DataIO_tools.save_data(saveRecons, saveDataPath)
            saveParamsPath = os.path.join(saveFolder, saveName + '_DeconvolutionParameters.json')
            saveParamDict = {'Data Parameters': dataPropertiesDict,
                             'Image formation model parameters': imFormationModelParameters,
                             'Algorithmic parameters': algOptionsDict}
            with open(saveParamsPath, 'w') as fp:
                json.dump(saveParamDict, fp, indent=4)
                fp.close()

        return finalReconstruction

    def checkData(self, data):
        #ToDo: Insert relevent checks here
        if np.min(data) < 0:
            return False
        else:
            return True

dataPropertiesDict = {'Camera pixel size [nm]': 95.7,
                      'Camera offset': 200,
                      'Scan step size [nm]': 105,
                      'Tilt angle [deg]': 35,
                      'Scan axis': 0,
                      'Tilt axis': 2,
                      'Data stacking': 'PLSR Interleaved',
                      'Planes in cycle': 20,
                      'Cycles': 20,
                      'Pos/Neg scan direction': 'Pos',
                      'Correct first cycle': True,
                      'Correct pixel offsets': True}

imFormationModelParameters = {'Optical PSF path': r'\\storage3.ad.scilifelab.se\testalab\Andreas\SOLS\Scripts\PSF RW_1.26_100nmPx_101x101x101.tif',
                              'Confined sheet FWHM [nm]': 200,
                              'Read-out sheet FWHM [nm]': 1200,
                              'Background sheet ratio': 0.1}

algOptionsDict = {'Reconstruction voxel size [nm]': 50,
                  'Clip factor for kernel cropping': 0.01,
                  'Iterations': 25}

saveOptions = {'Save to disc': True,
               'Save mode': 'Final',
               'Progression mode': 'All',
               'Save folder': r'A:\GitHub\Deconvolution_module',
               'Save name': 'TestDecon'}

import matplotlib.pyplot as plt

deconvolver = Deconvolver()
deconvolver.setAndLoadData(r'ActinChromo_HeLa_N205S_cell7_plsr_rec_Orca.hdf5', dataPropertiesDict)
ppdata = deconvolver.DF.getPreprocessedData()
deconvolved = deconvolver.Deconvolve(imFormationModelParameters, algOptionsDict, saveOptions)

import napari
viewer = napari.Viewer()
new_layer = viewer.add_image(deconvolved, rgb=True)