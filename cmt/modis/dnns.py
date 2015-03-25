# -----------------------------------------------------------------------------
# Copyright * 2014, United States Government, as represented by the
# Administrator of the National Aeronautics and Space Administration. All
# rights reserved.
#
# The Crisis Mapping Toolkit (CMT) v1 platform is licensed under the Apache
# License, Version 2.0 (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0.
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.
# -----------------------------------------------------------------------------

import ee
import math

from cmt.mapclient_qt import addToMap
from cmt.util.miscUtilities import safe_get_info, get_permanent_water_mask
from modis_utilities import *
import ee_classifiers

'''
Contains the DNNS set of MODIS algorithms.
'''


#==============================================================


def dnns_diff(domain, b):
    '''The DNNS algorithm but faster because it approximates the initial CART classification with
        a simple difference based method.'''
    return dnns(domain, b, True)

def dnns(domain, b, use_modis_diff=False):
    '''Dynamic Nearest Neighbor Search adapted from the paper:
        "Li, Sun, Yu, et. al. "A new short-wave infrared (SWIR) method for
        quantitative water fraction derivation and evaluation with EOS/MODIS
        and Landsat/TM data." IEEE Transactions on Geoscience and Remote Sensing, 2013."
        
        The core idea of this algorithm is to compute local estimates of a "pure water"
        and "pure land" pixel and compute each pixel's water percentage as a mixed
        composition of those two pure spectral types.
    '''
    
    # This algorithm has some differences from the original paper implementation.
    #  The most signficant of these is that it does not make use of land/water/partial
    #  preclassifications like the original paper does.  The search range is also
    #  much smaller in order to make the algorithm run faster in Earth Engine.
    # - Running this with a tiny kernel (effectively treating the entire region
    #    as part of the kernel) might get the best results!

    # Parameters
    KERNEL_SIZE = 40 # The original paper used a 100x100 pixel box = 25,000 meters!
    
    AVERAGE_SCALE_METERS = 250 # This scale is used to compute averages over the entire region
    
    # Set up two square kernels of the same size
    # - These kernels define the search range for nearby pure water and land pixels
    kernel            = ee.Kernel.square(KERNEL_SIZE, 'pixels', False)
    kernel_normalized = ee.Kernel.square(KERNEL_SIZE, 'pixels', True)
    
    # Compute b1/b6 and b2/b6
    composite_image = b['b1'].addBands(b['b2']).addBands(b['b6'])
    
    # Use CART classifier to divide pixels up into water, land, and mixed.
    # - Mixed pixels are just low probability water/land pixels.
    if use_modis_diff:
        unflooded_b = compute_modis_indices(domain.unflooded_domain)
        water_mask = get_permanent_water_mask()
        thresholds = compute_binary_threshold(get_diff(unflooded_b), water_mask, domain.bounds, True)

        pureWater = modis_diff(domain, b, thresholds[0])
        pureLand = modis_diff(domain, b, thresholds[1]).Not()
        mixed = pureWater.Or(pureLand).Not()
    else:
        classes   = ee_classifiers.earth_engine_classifier(domain, b, 'Pegasos', {'classifier_mode' : 'probability'})
        pureWater = classes.gte(0.95)
        pureLand  = classes.lte(0.05)
        #addToMap(classes, {'min': -1, 'max': 1}, 'CLASSES')
        #raise Exception('DEBUG')
        mixed     = pureWater.Not().And(pureLand.Not())
    averageWater      = safe_get_info(pureWater.mask(pureWater).multiply(composite_image).reduceRegion(ee.Reducer.mean(), domain.bounds, AVERAGE_SCALE_METERS))
    averageWaterImage = ee.Image([averageWater['sur_refl_b01'], averageWater['sur_refl_b02'], averageWater['sur_refl_b06']])
    
    # For each pixel, compute the number of nearby pure water pixels
    pureWaterCount = pureWater.convolve(kernel)
    # Get mean of nearby pure water (b1,b2,b6) values for each pixel with enough pure water nearby
    MIN_PUREWATER_NEARBY = 1
    pureWaterRef = pureWater.multiply(composite_image).convolve(kernel).multiply(pureWaterCount.gte(MIN_PUREWATER_NEARBY)).divide(pureWaterCount)
    # For pixels that did not have enough pure water nearby, just use the global average water value
    pureWaterRef = pureWaterRef.add(averageWaterImage.multiply(pureWaterRef.Not()))

   
    # Compute a backup, global pure land value to use when pixels have none nearby.
    averagePureLand      = safe_get_info(pureLand.mask(pureLand).multiply(composite_image).reduceRegion(ee.Reducer.mean(), domain.bounds, AVERAGE_SCALE_METERS))
    #averagePureLand      = composite_image.mask(pureLand).reduceRegion(ee.Reducer.mean(), domain.bounds, AVERAGE_SCALE_METERS)
    
    averagePureLandImage = ee.Image([averagePureLand['sur_refl_b01'], averagePureLand['sur_refl_b02'], averagePureLand['sur_refl_b06']])
    
    # Implement equations 10 and 11 from the paper --> It takes many lines of code to compute the local land pixels!
    oneOverSix   = b['b1'].divide(b['b6'])
    twoOverSix   = b['b2'].divide(b['b6'])
    eqTenLeft    = oneOverSix.subtract( pureWaterRef.select('sur_refl_b01').divide(b['b6']) )
    eqElevenLeft = twoOverSix.subtract( pureWaterRef.select('sur_refl_b02').divide(b['b6']) )
    
    # For each pixel, grab all the ratios from nearby pixels
    nearbyPixelsOneOverSix = oneOverSix.neighborhoodToBands(kernel) # Each of these images has one band per nearby pixel
    nearbyPixelsTwoOverSix = twoOverSix.neighborhoodToBands(kernel)
    nearbyPixelsOne        = b['b1'].neighborhoodToBands(kernel)
    nearbyPixelsTwo        = b['b2'].neighborhoodToBands(kernel)
    nearbyPixelsSix        = b['b6'].neighborhoodToBands(kernel)
    
    # Find which nearby pixels meet the EQ 10 and 11 criteria
    eqTenMatches        = ( nearbyPixelsOneOverSix.gt(eqTenLeft   ) ).And( nearbyPixelsOneOverSix.lt(oneOverSix) )
    eqElevenMatches     = ( nearbyPixelsTwoOverSix.gt(eqElevenLeft) ).And( nearbyPixelsTwoOverSix.lt(twoOverSix) )
    nearbyLandPixels    = eqTenMatches.And(eqElevenMatches)
    
    # Find the average of the nearby matching pixels
    numNearbyLandPixels = nearbyLandPixels.reduce(ee.Reducer.sum())
    meanNearbyBandOne   = nearbyPixelsOne.multiply(nearbyLandPixels).reduce(ee.Reducer.sum()).divide(numNearbyLandPixels)
    meanNearbyBandTwo   = nearbyPixelsTwo.multiply(nearbyLandPixels).reduce(ee.Reducer.sum()).divide(numNearbyLandPixels)
    meanNearbyBandSix   = nearbyPixelsSix.multiply(nearbyLandPixels).reduce(ee.Reducer.sum()).divide(numNearbyLandPixels)

    # Pack the results into a three channel image for the whole region
    # - Use the global pure land calculation to fill in if there are no nearby equation matching pixels
    MIN_PURE_NEARBY = 1
    meanPureLand = meanNearbyBandOne.addBands(meanNearbyBandTwo).addBands(meanNearbyBandSix)
    meanPureLand = meanPureLand.multiply(numNearbyLandPixels.gte(MIN_PURE_NEARBY)).add( averagePureLandImage.multiply(numNearbyLandPixels.lt(MIN_PURE_NEARBY)) )

    # Compute the water fraction: (land[b6] - b6) / (land[b6] - water[b6])
    # - Ultimately, relying solely on band 6 for the final classification may not be a good idea!
    meanPureLandSix = meanPureLand.select('sum_2')
    water_fraction = (meanPureLandSix.subtract(b['b6'])).divide(meanPureLandSix.subtract(pureWaterRef.select('sur_refl_b06'))).clamp(0, 1)
       
    # Set pure water to 1, pure land to 0
    water_fraction = water_fraction.add(pureWater).subtract(pureLand).clamp(0, 1)
    
    #addToMap(fraction, {'min': 0, 'max': 1},   'fraction', False)
    #addToMap(pureWater,      {'min': 0, 'max': 1},   'pure water', False)
    #addToMap(pureLand,       {'min': 0, 'max': 1},   'pure land', False)
    #addToMap(mixed,          {'min': 0, 'max': 1},   'mixed', False)
    #addToMap(pureWaterCount, {'min': 0, 'max': 300}, 'pure water count', False)
    #addToMap(water_fraction, {'min': 0, 'max': 5},   'water_fractionDNNS', False)
    #addToMap(pureWaterRef,   {'min': 0, 'max': 3000, 'bands': ['sur_refl_b01', 'sur_refl_b02', 'sur_refl_b06']}, 'pureWaterRef', False)

    return water_fraction.select(['sum_2'], ['b1']) # Rename sum_2 to b1

def dnns_diff_dem(domain, b):
    '''The DNNS DEM algorithm but faster because it approximates the initial CART classification with
        a simple difference based method.'''
    return dnns_dem(domain, b, True)

def dnns_dem(domain, b, use_modis_diff=False):
    '''Enhance the DNNS result with high resolution DEM information, adapted from the paper:
        "Li, Sun, Goldberg, and Stefanidis. "Derivation of 30-m-resolution
        water maps from TERRA/MODIS and SRTM." Remote Sensing of Environment, 2013."
        
        This enhancement to the DNNS_DEM algorithm combines a higher resolution DEM with
        input pixels that have a percent flooded value.  Based on the percentage and DEM
        values, the algorithm classifies each higher resolution DEM pixel as either
        flooded or dry.
        '''
    
    MODIS_PIXEL_SIZE_METERS = 250
    
    # Call the DNNS function to get the starting point
    water_fraction = dnns(domain, b, use_modis_diff)

   
    ## Treating the DEM values contained in the MODIS pixel as a histogram, find the N'th percentile
    ##  where N is the water fraction computed by DNNS.  That should be the height of the flood water.
    #modisPixelKernel = ee.Kernel.square(MODIS_PIXEL_SIZE_METERS, 'meters', False)
    #dem.mask(water_fraction).reduceNeighborhood(ee.Reducer.percentile(), modisPixelKernel)
    # --> We would like to compute a percentile here, but this would require a different reducer input for each pixel!
    
    # Get whichever DEM is loaded in the domain
    dem = domain.get_dem().image
    
    # Get min and max DEM height within each water containing pixel
    # - If a DEM pixel contains any water then the water level must be at least that high.    
    dem_min = dem.mask(water_fraction).focal_min(MODIS_PIXEL_SIZE_METERS, 'square', 'meters')
    dem_max = dem.mask(water_fraction).focal_max(MODIS_PIXEL_SIZE_METERS, 'square', 'meters')
    
    # Approximation, linearize each tile's fraction point
    # - The water percentage is used as a percentage between the two elevations
    # - Don't include full or empty pixels, they don't give us clues to their height.
    water_high = dem_min.add(dem_max.subtract(dem_min).multiply(water_fraction))
    water_high = water_high.multiply(water_fraction.lt(1.0)).multiply(water_fraction.gt(0.0)) 
    
    # Problem: Averaging process spreads water way out to pixels where it was not detected!!
    #          Reducing the averaging is a simple way to deal with this and probably does not hurt results at all
    
    #dilate_kernel = ee.Kernel.circle(250, 'meters', False)
    #allowed_water_mask = water_fraction.gt(0.0)#.convolve(dilate_kernel)
    
    ## Smooth out the water elevations with a broad kernel; nearby pixels probably have the same elevation!
    water_dem_kernel        = ee.Kernel.circle(5000, 'meters', False)
    num_nearby_water_pixels = water_high.gt(0.0).convolve(water_dem_kernel)
    average_high            = water_high.convolve(water_dem_kernel).divide(num_nearby_water_pixels)
    
    # Alternate smoothing method using available tool EE
    water_present   = water_fraction.gt(0.0)#.mask(water_high)
    #connected_water = ee.Algorithms.ConnectedComponentLabeler(water_present, water_dem_kernel, 256) # Perform blob labeling
    #addToMap(water_present,   {'min': 0, 'max':   1}, 'water present', False);
    #addToMap(connected_water, {'min': 0, 'max': 256}, 'labeled blobs', False);
    
    #addToMap(water_fraction, {'min': 0, 'max':   1}, 'Water Fraction', False);
    #addToMap(average_high, {min:25, max:40}, 'Water Level', false);
    #addToMap(dem.subtract(average_high), {min : -0, max : 10}, 'Water Difference', false);
    #addToMap(dem.lte(average_high).and(domain.groundTruth.not()));
    
    #addToMap(allowed_water_mask, {'min': 0, 'max': 1}, 'allowed_water', False);
    #addToMap(water_high, {'min': 0, 'max': 100}, 'water_high', False);
    #addToMap(average_high, {'min': 0, 'max': 100}, 'average_high', False);
    #addToMap(dem, {'min': 0, 'max': 100}, 'DEM', False);
    
    # Classify DEM pixels as flooded based on being under the local water elevation or being completely flooded.
    #return dem.lte(average_high).Or(water_fraction.eq(1.0)).select(['elevation'], ['b1'])
    dem_water = dem.lte(average_high).mask(water_fraction) # Mask prevents pixels with 0% water from being labeled as water
    return dem_water.Or(water_fraction.eq(1.0)).select(['elevation'], ['b1'])


#=====================================================================================

# This function is for testing modifications to the DNNS algorithmm
def dnns_revised(domain, b):
    '''Dynamic Nearest Neighbor Search with revisions to improve performance on our test data'''
    
    # One issue with this algorithm is that its large search range slows down even Earth Engine!
    # - With a tiny kernel size, everything is relative to the region average which seems to work pretty well.
    # Another problem is that we don't have a good way of identifying 'Definately land' pixels like we do for water.
    
    # Parameters
    KERNEL_SIZE = 1 # The original paper used a 100x100 pixel box = 25,000 meters!
    PURELAND_THRESHOLD = 3500 # TODO: Vary by domain?
    PURE_WATER_THRESHOLD_RATIO = 0.1
    
    # Set up two square kernels of the same size
    # - These kernels define the search range for nearby pure water and land pixels
    kernel            = ee.Kernel.square(KERNEL_SIZE, 'pixels', False)
    kernel_normalized = ee.Kernel.square(KERNEL_SIZE, 'pixels', True)
    
    composite_image = b['b1'].addBands(b['b2']).addBands(b['b6'])
    
    # Compute (b2 - b1) < threshold, a simple water detection algorithm.  Treat the result as "pure water" pixels.
    pureWaterThreshold = float(domain.algorithm_params['modis_diff_threshold']) * PURE_WATER_THRESHOLD_RATIO
    pureWater = modis_diff(domain, b, pureWaterThreshold)
    
    # Compute the mean value of pure water pixels across the entire region, then store in a constant value image.
    AVERAGE_SCALE_METERS = 30 # This value seems to have no effect on the results
    averageWater      = safe_get_info(pureWater.mask(pureWater).multiply(composite_image).reduceRegion(ee.Reducer.mean(), domain.bounds, AVERAGE_SCALE_METERS))
    averageWaterImage = ee.Image([averageWater['sur_refl_b01'], averageWater['sur_refl_b02'], averageWater['sur_refl_b06']])
    
    # For each pixel, compute the number of nearby pure water pixels
    pureWaterCount = pureWater.convolve(kernel)
    # Get mean of nearby pure water (b1,b2,b6) values for each pixel with enough pure water nearby
    MIN_PURE_NEARBY = 10
    averageWaterLocal = pureWater.multiply(composite_image).convolve(kernel).multiply(pureWaterCount.gte(MIN_PURE_NEARBY)).divide(pureWaterCount)
    # For pixels that did not have enough pure water nearby, just use the global average water value
    averageWaterLocal = averageWaterLocal.add(averageWaterImage.multiply(averageWaterLocal.Not()))

   
    # Use simple diff method to select pure land pixels
    #LAND_THRESHOLD   = 2000 # TODO: Move to domain selector
    pureLand             = b['b2'].subtract(b['b1']).gte(PURELAND_THRESHOLD).select(['sur_refl_b02'], ['b1']) # Rename sur_refl_b02 to b1
    averagePureLand      = safe_get_info(pureLand.mask(pureLand).multiply(composite_image).reduceRegion(ee.Reducer.mean(), domain.bounds, AVERAGE_SCALE_METERS))
    averagePureLandImage = ee.Image([averagePureLand['sur_refl_b01'], averagePureLand['sur_refl_b02'], averagePureLand['sur_refl_b06']])
    pureLandCount        = pureLand.convolve(kernel)        # Get nearby pure land count for each pixel
    averagePureLandLocal = pureLand.multiply(composite_image).convolve(kernel).multiply(pureLandCount.gte(MIN_PURE_NEARBY)).divide(pureLandCount)
    averagePureLandLocal = averagePureLandLocal.add(averagePureLandImage.multiply(averagePureLandLocal.Not())) # For pixels that did not have any pure land nearby, use mean


    # Implement equations 10 and 11 from the paper
    oneOverSix   = b['b1'].divide(b['b6'])
    twoOverSix   = b['b2'].divide(b['b6'])
    eqTenLeft    = oneOverSix.subtract( averageWaterLocal.select('sur_refl_b01').divide(b['b6']) )
    eqElevenLeft = twoOverSix.subtract( averageWaterLocal.select('sur_refl_b02').divide(b['b6']) )
    
    # For each pixel, grab all the ratios from nearby pixels
    nearbyPixelsOneOverSix = oneOverSix.neighborhoodToBands(kernel) # Each of these images has one band per nearby pixel
    nearbyPixelsTwoOverSix = twoOverSix.neighborhoodToBands(kernel)
    nearbyPixelsOne        = b['b1'].neighborhoodToBands(kernel)
    nearbyPixelsTwo        = b['b2'].neighborhoodToBands(kernel)
    nearbyPixelsSix        = b['b6'].neighborhoodToBands(kernel)
    
    # Find which nearby pixels meet the EQ 10 and 11 criteria
    eqTenMatches        = ( nearbyPixelsOneOverSix.gt(eqTenLeft   ) ).And( nearbyPixelsOneOverSix.lt(oneOverSix) )
    eqElevenMatches     = ( nearbyPixelsTwoOverSix.gt(eqElevenLeft) ).And( nearbyPixelsTwoOverSix.lt(twoOverSix) )
    nearbyLandPixels    = eqTenMatches.And(eqElevenMatches)
    
    # Find the average of the nearby matching pixels
    numNearbyLandPixels = nearbyLandPixels.reduce(ee.Reducer.sum())
    meanNearbyBandOne   = nearbyPixelsOne.multiply(nearbyLandPixels).reduce(ee.Reducer.sum()).divide(numNearbyLandPixels)
    meanNearbyBandTwo   = nearbyPixelsTwo.multiply(nearbyLandPixels).reduce(ee.Reducer.sum()).divide(numNearbyLandPixels)
    meanNearbyBandSix   = nearbyPixelsSix.multiply(nearbyLandPixels).reduce(ee.Reducer.sum()).divide(numNearbyLandPixels)


    # Pack the results into a three channel image for the whole region
    meanNearbyLand = meanNearbyBandOne.addBands(meanNearbyBandTwo).addBands(meanNearbyBandSix)
    meanNearbyLand = meanNearbyLand.multiply(numNearbyLandPixels.gte(MIN_PURE_NEARBY)).add( averagePureLandImage.multiply(numNearbyLandPixels.lt(MIN_PURE_NEARBY)) )

    addToMap(numNearbyLandPixels,  {'min': 0, 'max': 400, }, 'numNearbyLandPixels', False)
    addToMap(meanNearbyLand,       {'min': 0, 'max': 3000, 'bands': ['sum', 'sum_1', 'sum_2']}, 'meanNearbyLand', False)

    
    # Compute the water fraction: (land - b) / (land - water)
    landDiff  = averagePureLandLocal.subtract(composite_image)
    waterDiff = averageWaterLocal.subtract(composite_image)
    typeDiff  = averagePureLandLocal.subtract(averageWaterLocal)
    #water_vector   = (averageLandLocal.subtract(b)).divide(averageLandLocal.subtract(averageWaterLocal))
    landDist  = landDiff.expression("b('sur_refl_b01')*b('sur_refl_b01') + b('sur_refl_b02') *b('sur_refl_b02') + b('sur_refl_b06')*b('sur_refl_b06')").sqrt();
    waterDist = waterDiff.expression("b('sur_refl_b01')*b('sur_refl_b01') + b('sur_refl_b02') *b('sur_refl_b02') + b('sur_refl_b06')*b('sur_refl_b06')").sqrt();
    typeDist  = typeDiff.expression("b('sur_refl_b01')*b('sur_refl_b01') + b('sur_refl_b02') *b('sur_refl_b02') + b('sur_refl_b06')*b('sur_refl_b06')").sqrt();
       
    #waterOff = landDist.divide(waterDist.add(landDist)) 
    waterOff = landDist.divide(typeDist) # TODO: Improve this math, maybe full matrix treatment?

    # Set pure water to 1, pure land to 0
    waterOff = waterOff.subtract(pureLand.multiply(waterOff))
    waterOff = waterOff.add(pureWater.multiply(ee.Image(1.0).subtract(waterOff)))
    
    # TODO: Better way of filtering out low fraction pixels.
    waterOff = waterOff.multiply(waterOff)
    waterOff = waterOff.gt(0.6)
    
    #addToMap(fraction,       {'min': 0, 'max': 1},   'fraction', False)
    addToMap(pureWater,      {'min': 0, 'max': 1},   'pure water', False)
    addToMap(pureLand,       {'min': 0, 'max': 1},   'pure land', False)
    addToMap(pureWaterCount, {'min': 0, 'max': 100}, 'pure water count', False)
    addToMap(pureLandCount,  {'min': 0, 'max': 100}, 'pure land count', False)
    
    
    #addToMap(numNearbyLandPixels,  {'min': 0, 'max': 400, }, 'numNearbyLandPixels', False)
    #addToMap(meanNearbyLand,       {'min': 0, 'max': 3000, 'bands': ['sum', 'sum_1', 'sum_2']}, 'meanNearbyLand', False)
    addToMap(averageWaterImage,    {'min': 0, 'max': 3000, 'bands': ['constant', 'constant_1', 'constant_2']}, 'average water', False)
    addToMap(averagePureLandImage, {'min': 0, 'max': 3000, 'bands': ['constant', 'constant_1', 'constant_2']}, 'average pure land',  False)
    addToMap(averageWaterLocal,    {'min': 0, 'max': 3000, 'bands': ['sur_refl_b01', 'sur_refl_b02', 'sur_refl_b06']}, 'local water ref', False)
    addToMap(averagePureLandLocal, {'min': 0, 'max': 3000, 'bands': ['sur_refl_b01', 'sur_refl_b02', 'sur_refl_b06']}, 'local pure land ref',  False)
    
    
    return waterOff.select(['sur_refl_b01'], ['b1']) # Rename sur_refl_b02 to b1


