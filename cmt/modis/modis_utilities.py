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
from cmt.util.evaluation import safe_get_info

'''
Utility functions helpful for many MODIS algorithms
'''


def compute_modis_indices(domain):
    '''Compute several common interpretations of the MODIS bands'''
    
    band1 = domain.modis.sur_refl_b01 # pRED
    band2 = domain.modis.sur_refl_b02 # pNIR

    # Other bands must be used at lower resolution
    band3 = domain.modis.sur_refl_b03 # pBLUE
    band4 = domain.modis.sur_refl_b04
    band5 = domain.modis.sur_refl_b05
    band6 = domain.modis.sur_refl_b06 # pSWIR

    NDVI = (band2.subtract(band1)).divide(band2.add(band1));
    # Normalized difference water index
    NDWI = (band1.subtract(band6)).divide(band1.add(band6));
    # Enhanced vegetation index
    EVI = band2.subtract(band1).multiply(2.5).divide( band2.add(band1.multiply(6)).subtract(band3.multiply(7.5)).add(1));
    # Land surface water index
    LSWI = (band2.subtract(band6)).divide(band2.add(band6));
    # Convenience measure
    DVEL = EVI.subtract(LSWI)

    return {'b1': band1, 'b2': band2, 'b3': band3, 'b4' : band4, 'b5' : band5, 'b6': band6,
            'NDVI': NDVI, 'NDWI': NDWI, 'EVI': EVI, 'LSWI': LSWI, 'DVEL': DVEL,
            'pRED': band1, 'pNIR': band2, 'pBLUE': band3, 'pSWIR': band6}



def getQABits(image, start, end, newName):
    '''Extract bits from positions "start" to "end" in the image'''
    # Create a bit mask of the bits we need
    pattern = 0
    for i in range(start,end):
       pattern += 2**i
    # Extract the bits, shift them over, and rename the channel.
    temp = ee.Image(pattern)
    return image.select([0], [newName]).bitwise_and(temp).rightShift(start)

def getModisBadPixelMask(lowResModis):
    '''Retrieves the 1km MODIS bad pixel mask (identifies clouds)'''

    # Select the QA band
    qaBand = lowResModis.select('state_1km').uint16()
   
    # Get the cloud_state bits and find cloudy areas.
    cloudBits = getQABits(qaBand, 0, 1, 'cloud_state')
    cloud = cloudBits.eq(1).Or(cloudBits.eq(2))

    return cloud # The second part of this, the land water flag, does not work well at all.
    
    ## Get the land_water_flag bits.
    #landWaterFlag = getQABits(qaBand, 3, 5, 'land_water_flag')
    #
    ## Create a mask that filters out deep ocean and cloudy areas.
    #mask = landWaterFlag.neq(7).And(cloud.Not())
    #return mask

def getCloudPercentage(lowResModis, region):
    '''Returns the percentage of a region flagged as clouds by the MODIS metadata'''

    MODIS_CLOUD_RESOLUTION = 1000 # Clouds are flagged at this resolution

    # Divide the number of cloud pixels by the total number of pixels
    oneMask    = ee.Image(1.0) 
    cloudMask  = getModisBadPixelMask(lowResModis)
    areaCount  = oneMask.reduceRegion(  ee.Reducer.sum(), region, MODIS_CLOUD_RESOLUTION)
    cloudCount = cloudMask.reduceRegion(ee.Reducer.sum(), region, MODIS_CLOUD_RESOLUTION)
    percentage = safe_get_info(cloudCount)['cloud_state'] / safe_get_info(areaCount)['constant']
    print 'Detected cloud percentage: ' + str(percentage)
    return percentage

def get_permanent_water_mask():
    return ee.Image("MODIS/MOD44W/MOD44W_005_2000_02_24").select(['water_mask'], ['b1'])



# If mixed_thresholds is true, we find the thresholds that contain 0.05 land and 0.95 water
def compute_binary_threshold(valueImage, classification, bounds, mixed_thresholds=False):
    '''Computes a threshold for a value given examples in a classified binary image'''
    
    # Build histograms of the true and false labeled values
    valueInFalse   = valueImage.mask(classification.Not())
    valueInTrue    = valueImage.mask(classification)
    NUM_BINS       = 128
    SCALE          = 250 # In meters
    histogramFalse = safe_get_info(valueInFalse.reduceRegion(ee.Reducer.histogram(NUM_BINS, None, None), bounds, SCALE))['b1']
    histogramTrue  = safe_get_info(valueInTrue.reduceRegion( ee.Reducer.histogram(NUM_BINS, None, None), bounds, SCALE))['b1']
    
    # Get total number of pixels in each histogram
    false_total = sum(histogramFalse['histogram'])
    true_total  = sum(histogramTrue[ 'histogram'])
    
    # WARNING: This method assumes that the false histogram is composed of greater numbers than the true histogram!!
    #        : This happens to be the case for the three algorithms we are currently using this for.
    
    false_index = 0
    false_sum   = false_total
    true_sum    = 0.0
    threshold_index = None
    lower_mixed_index = None
    upper_mixed_index = None
    for i in range(len(histogramTrue['histogram'])): # Iterate through the bins of the true histogram
        # Add the number of pixels in the current true bin
        true_sum += histogramTrue['histogram'][i]
        
        # Set x equal to the max end of the current bin
        x = histogramTrue['bucketMin'] + (i+1)*histogramTrue['bucketWidth']
        
        # Determine the bin of the false histogram that x falls in
        # - Also update the number of 
        while ( (false_index < len(histogramFalse['histogram'])) and
                (histogramFalse['bucketMin'] + false_index*histogramFalse['bucketWidth'] < x) ):
            false_sum   -= histogramFalse['histogram'][false_index] # Remove the pixels from the current false bin
            false_index += 1 # Move to the next bin of the false histogram
    
        percent_true_under_thresh = true_sum/true_total
        percent_false_over_thresh = false_sum/false_total
            
        if mixed_thresholds:
            if (false_total - false_sum) / float(true_sum) <= 0.05:
                lower_mixed_index = i
            if upper_mixed_index == None and (true_total - true_sum) / float(false_sum) <= 0.05:
                upper_mixed_index = i
        else:
            if threshold_index == None and (percent_false_over_thresh < percent_true_under_thresh) and (percent_true_under_thresh > 0.5):
                break

    
    if mixed_thresholds:
        lower = histogramTrue['bucketMin'] + lower_mixed_index * histogramTrue['bucketWidth'] + histogramTrue['bucketWidth']/2
        upper = histogramTrue['bucketMin'] + upper_mixed_index * histogramTrue['bucketWidth'] + histogramTrue['bucketWidth']/2
        if lower > upper:
            temp = lower
            lower = upper
            upper = temp
        print 'Thresholds (%g, %g) found.' % (lower, upper)
        return (lower, upper)
    else:
        # Put threshold in the center of the current true histogram bin/bucket
        threshold = histogramTrue['bucketMin'] + i*histogramTrue['bucketWidth'] + histogramTrue['bucketWidth']/2
        print 'Threshold %g Found. %g%% of water pixels and %g%% of land pixels separated.' % \
            (threshold, true_sum / true_total * 100.0, false_sum / false_total * 100.0)
        return threshold


def compute_dem_slope_degrees(dem, resolution):
    '''Computes a slope in degrees for each pixel of the DEM'''
    
    deriv = dem.derivative()
    dZdX    = deriv.select(['elevation_x']).divide(resolution)
    dZdY    = deriv.select(['elevation_y']).divide(resolution)
    slope = dZdX.multiply(dZdX).add(dZdY.multiply(dZdY)).sqrt().reproject("EPSG:4269", None, resolution); 
    RAD2DEG = 180 / 3.14159
    slopeAngle = slope.atan().multiply(RAD2DEG);
    return slopeAngle

