
#
# Copyright 2014-2015 University of Southern California
# Distributed under the (new) BSD License. See LICENSE.txt for more info.
#

import numpy as np

import datetime

import os
import math
import csv

import volspy.viewer as base

from analyze import BlockedAnalyzerOpt, assign_voxels_opt, compose_3d_kernel, gaussian_kernel
from volspy.util import bin_reduce

import tifffile
        
_color_uniforms = """
uniform int u_numchannels;
uniform float u_gain;
uniform float u_floorlvl;
uniform float u_nuclvl;
uniform float u_msklvl;
uniform float u_zerlvl;
uniform float u_toplvl;
uniform float u_transp;
"""

# R: raw signal
# G: synapse core samples
# B: synapse vicinity samples
# A: auto-fluorescence signal and synapse mask sample

_linear1_colorxfer = """
       if (col_smp.a > u_msklvl) {
          col_smp.a = (col_smp.a - u_zerlvl) / u_toplvl;
          col_smp.r = 1.0;
          col_smp.g = 0.0;
          col_smp.b = 0.0;
       }
       else if ((col_smp.r - u_zerlvl) > u_toplvl) {
          col_smp.a = u_transp * 0.1;
          col_smp.r = 0.5;
          col_smp.b = 0.5;
          col_smp.g = 0.0;
       }
       else if (col_smp.b < u_nuclvl && col_smp.g > u_floorlvl) {
          col_smp.a = u_transp * (col_smp.r - u_zerlvl) / (u_toplvl - u_zerlvl);
          col_smp.r = 0.0;
          col_smp.g = 1.0;
          col_smp.b = 0.0;
       }
       else {
          col_smp.a = u_transp * (col_smp.r - u_zerlvl) / (u_toplvl - u_zerlvl);
          col_smp.r = 0.0;
          col_smp.g = 0.0;
          col_smp.b = 1.0;
       }

       col_smp = clamp( u_gain * col_smp, 0.0, 1.0);
"""

_linear_alpha = """
"""

_binary1_colorxfer = """
       if (col_smp.a > u_msklvl) {
          col_smp.a = (col_smp.a - u_zerlvl) / u_toplvl;
          col_smp.r = 1.0;
          col_smp.g = 0.0;
          col_smp.b = 0.0;
       }
       else if ((col_smp.r - u_zerlvl) > u_toplvl) {
          col_smp.a = u_transp * 0.1;
          col_smp.r = 0.5;
          col_smp.b = 0.5;
          col_smp.g = 0.0;
       }
       else if (col_smp.b < u_nuclvl && col_smp.g > u_floorlvl) {
          col_smp.a = 1.0;
          col_smp.r = 0.0;
          col_smp.g = 1.0;
          col_smp.b = 0.0;
       }
       else {
          col_smp.a = u_transp * (col_smp.r - u_zerlvl) / (u_toplvl - u_zerlvl);
          col_smp.r = 0.0;
          col_smp.g = 0.0;
          col_smp.b = 1.0;
       }

       col_smp = clamp( u_gain * col_smp, 0.0, 1.0);
"""

_binary_alpha = ""

def adjust_level(uniform, attribute, step=0.0005, altstep=None, trace="%(uniform)s level set to %(level).5f", tracenorm=True):
    def helper(origmethod):
        def wrapper(*args):
            self = args[0]
            event = args[1]
            level = getattr(self, attribute)

            if 'Alt' in event.modifiers:
                if altstep is not None:
                    delta = altstep
                else:
                    delta = 0.1 * step
                    
            if 'Shift' in event.modifiers:
                level += step
            else:
                level -= step

            setattr(self, attribute, level)
            
            self.volume_renderer.set_uniform(uniform, level)
            self.update()

            if trace:
                if tracenorm:
                    level = level * (self.data_max - self.data_min) + self.data_min
                print trace % dict(uniform=uniform, level=level)

        return wrapper
    return helper

class Canvas(base.Canvas):

    _vol_interp = 'nearest'
    #_vol_interp = 'linear'
    
    def _reform_image(self, I, meta):
        analyzer = BlockedAnalyzerOpt(I, self.synapse_diam_microns, self.vicinity_diam_microns, self.redblur_microns)
        self.raw_image = I

        splits = [(datetime.datetime.now(), None)]
        
        view_image, centroids, centroid_measures = analyzer.volume_process()
        splits.append((datetime.datetime.now(), 'volume process'))

        def rinfo(a):
            return (a.min(), a.mean(), a.max())
        
        # get labeled voxels
        centroid_measures = np.array(centroid_measures, dtype=np.float32)
        print "core range:", rinfo(centroid_measures[:,0])
        print "hollow range:", rinfo(centroid_measures[:,1])
        centroids2 = np.array(centroids, dtype=np.int32) / np.array(analyzer.view_reduction, dtype=np.int32)
        print "centroid2 range:", [rinfo(v) for v in [centroids2[0], centroids2[1], centroids2[2]]]
        print "view_image shape:", view_image.shape
        splat_kern = compose_3d_kernel(map(
            lambda d, s, r: gaussian_kernel(d/s/6./r),
            self.synapse_diam_microns,
            I.micron_spacing,
            analyzer.view_reduction
        ))
        splat_kern /= splat_kern.sum()
        print "segment map splat kernel", splat_kern.shape, splat_kern.sum(), splat_kern.max()
        segment_map = assign_voxels_opt(
            centroid_measures[:,0],
            centroids2,
            view_image.shape[0:3],
            splat_kern
        )
        splits.append((datetime.datetime.now(), 'segment map'))

        # fill segmented voxels w/ per-segment measurements
        syn_val_map = np.array([0] + list(centroid_measures[:,0])).astype(I.dtype)[segment_map]
        vcn_val_map = np.array([0] + list(centroid_measures[:,1])).astype(I.dtype)[segment_map]
        if centroid_measures.shape[1] > 2:
            msk_channel = np.array([0] + list(centroid_measures[:,2])).astype(I.dtype)[segment_map]
        splits.append((datetime.datetime.now(), 'segment measures splat'))

        result = np.zeros( view_image.shape[0:3] + (4,), dtype=I.dtype )
        result[:,:,:,0] = view_image[:,:,:,0]
        result[:,:,:,1] = syn_val_map
        result[:,:,:,2] = vcn_val_map
        if centroid_measures.shape[1] > 4:
            result[:,:,:,3] = msk_channel + view_image[:,:,:,1] * (segment_map==0)
        splits.append((datetime.datetime.now(), 'segmented volume assemble'))
            
        self.data_max = result.max()
        self.data_min = result.min()

        perf_vector = map(lambda t0, t1: ((t1[0]-t0[0]).total_seconds(), t1[1]), splits[0:-1], splits[1:])
        for elapsed, desc in perf_vector:
            print "%8.2fs %s task time" % (elapsed, desc)
        
        print "measure counts:", centroid_measures.shape
        print "map range:", segment_map.min(), segment_map.max()
        print "packed data range: ", self.data_min, self.data_max

        segment_map = None

        self.analyzer = analyzer
        self.syn_values = centroid_measures[:,0]
        self.vcn_values = centroid_measures[:,1]
        if centroid_measures.shape[1] > 4:
            self.red_values = centroid_measures[:,2]
        else:
            self.red_values = np.zeros((centroid_measures.shape[0],), dtype=np.float32)
        self.centroids = centroids
        self.centroid_measures = centroid_measures
        #self.widths = widths

        return result, analyzer.view_reduction

    _frag_glsl_dicts = [
        dict(
            uniforms=_color_uniforms,
            colorxfer=_linear1_colorxfer,
            alphastmt=_linear_alpha,
            desc='White-linear segments, blue-linear background, and red-linear mask channel.'
            ),
        dict(
            uniforms=_color_uniforms,
            colorxfer=_binary1_colorxfer,
            alphastmt=_binary_alpha,
            desc='White-boolean segments, blue-linear background, and red-boolean mask channel.'
            )
        ]

    def __init__(self, filename1):
        
        # TODO: put these under UI control?
        self.synapse_diam_microns = (2.75, 1.5, 1.5)
        self.vicinity_diam_microns = (4.0, 2.75, 2.75)

        self.redblur_microns = (3.0, 3.0, 3.0)

        base.Canvas.__init__(self, filename1)

        self.key_press_handlers['N'] = self.adjust_nuc_level
        self.key_press_handlers['M'] = self.adjust_msk_level
        self.key_press_handlers['T'] = self.adjust_zer_level
        self.key_press_handlers['U'] = self.adjust_top_level
        self.key_press_handlers['O'] = self.adjust_transp_level
        self.key_press_handlers['D'] = self.dump_params_or_classified
        self.key_press_handlers['H'] = self.dump_segment_heatmap
        self.key_press_handlers['?'] = self.help

        self.size = 512, 512

    def reload_data(self):
        base.Canvas.reload_data(self)

    def reset_ui(self, event=None):
        """Reset UI controls to startup state."""
        self.nuclvl = (1.2*self.vcn_values.mean()-self.data_min) / (self.data_max-self.data_min)
        self.msklvl = (self.red_values.max()-self.data_min) / (self.data_max-self.data_min) or 1.0
        self.zerlvl = 0.28
        self.toplvl = 0.4
        self.transp = 0.8

        self.volume_renderer.set_uniform('u_nuclvl', self.nuclvl)
        self.volume_renderer.set_uniform('u_msklvl', self.msklvl)
        self.volume_renderer.set_uniform('u_zerlvl', self.zerlvl)
        self.volume_renderer.set_uniform('u_toplvl', self.toplvl)
        self.volume_renderer.set_uniform('u_transp', self.transp)
        base.Canvas.reset_ui(self, event)
        self.floorlvl = (0.9*self.syn_values.mean()-self.data_min) / (self.data_max-self.data_min)
        self.volume_renderer.set_uniform('u_floorlvl', self.floorlvl)

    def dump_parameters(self, event):
        """Dump current parameters."""
        print """
gain: %f
zoom: %f
color mode: %d %s

small feature threshold: %f
nuclear feature threshold: %f
red mask threshold: %f
zero crossing threshold: %f
upper clipping threshold: %f
transparency factor: %f
""" % (
            self.gain,
            self.zoom,
            self.volume_renderer.color_mode, 
            self._frag_glsl_dicts[self.volume_renderer.color_mode].get('desc', 'UNKNOWN'),
            self.floorlvl * (self.data_max - self.data_min) + self.data_min,
            self.nuclvl * (self.data_max - self.data_min) + self.data_min,
            self.msklvl * (self.data_max - self.data_min) + self.data_min,
            self.zerlvl * (self.data_max - self.data_min) + self.data_min,
            self.toplvl * (self.data_max - self.data_min) + self.data_min,
            self.transp
            )

    def dump_params_or_classified(self, event):
        """Dump current parameters ('d') or voxel classification ('D')."""
        if 'Shift' in event.modifiers:
            self.dump_classified_voxels(event)
        else:
            self.dump_parameters(event)

    def dump_classified_voxels(self, event):
        """Dump a volume image with centroid markers."""
        dtype = np.uint16
        dmax = 1./self.raw_image.max() * (2**16-1)

        result = np.zeros( self.raw_image.shape[0:3] + (3,), dtype ) # RGB debug image
        result[:,:,:,2] = self.raw_image[:,:,:,0] * dmax

        for centroid in self.centroids:
            result[tuple(
                slice(c, c+1)
                for c in centroid
            ) + (1,)] = (2**16-1)

        result = np.sqrt(result)
        result = (result * ((2**8-1)/result.max())).astype(np.uint8)
            
        tifffile.imsave('/scratch/debug.tiff', result)
        print "/scratch/debug.tiff dumped"

        csvfile = open('/scratch/segments.csv', 'w')
        writer = csv.writer(csvfile)
        writer.writerow(
            ('Z', 'Y', 'X', 'raw core', 'raw hollow', 'DoG core', 'DoG hollow')
            + ((self.centroid_measures.shape[1] == 5) and ('red',) or ())
        )
        for i in range(self.centroid_measures.shape[0]):
            Z, Y, X = self.centroids[i]
            writer.writerow( 
                (Z, Y, X) + tuple(self.centroid_measures[i,m] for m in range(self.centroid_measures.shape[1]))
            )
        del writer
        csvfile.close()
        print "/scratch/segments.csv dumped"

    def dump_segment_heatmap(self, event):
        """Dump a heatmap image with current thresholds."""

        IMAGE_SIZE = 1024
        HM_GAIN = 64

        MAX_VAL = max(self._all_segments[0].max(), self._all_segments[1].max())
        MAX_SYN = 80000
        MAX_VCN = 80000

        # correct for OpenGL texel normalization
        syn_lvl = self.floorlvl * (self.data_max - self.data_min) + self.data_min
        vcn_lvl = self.nuclvl * (self.data_max - self.data_min) + self.data_min

        heatmap = np.zeros((IMAGE_SIZE, IMAGE_SIZE, 3), dtype=np.float32)

        def reanalyze():
            return self.analyzer.analyze(
                self._all_segments[0],
                self._all_segments[1],
                syn_lvl,
                vcn_lvl
            )

        def render(syn_vals, vcn_vals, ch):
            print "rendering %d segments to heatmap" % len(syn_vals)
            for i in range(len(syn_vals)):
                if syn_vals[i] > 0:
                    x = min(IMAGE_SIZE-1,math.log(syn_vals[i]) * IMAGE_SIZE / math.log(MAX_SYN))
                    #x = min(IMAGE_SIZE-1,syn_vals[i] * IMAGE_SIZE / MAX_SYN)
                    if vcn_vals[i] > 0:
                        y = min(IMAGE_SIZE-1,math.log(vcn_vals[i]) * IMAGE_SIZE / math.log(MAX_VCN))
                        #y = min(IMAGE_SIZE-1,vcn_vals[i] * IMAGE_SIZE / MAX_VCN)
                    else:
                        y = 0
                    heatmap[y,x,ch] += 1
                    #heatmap = np.sqrt(heatmap)

            histo = []
            hmax = heatmap.max()
            print "heatmap max:", hmax
            for i in range(hmax):
                s = np.sum((heatmap > i) & (heatmap <= (i+1)))
                histo.append(s)

            max_bins = []
            for j in range(IMAGE_SIZE):
                for i in range(IMAGE_SIZE):
                    if heatmap[j,i,0] == hmax:
                        max_bins.append( (i, j) )

            print "heatmap max %s at XY bins %s" % (hmax, max_bins)
            print "heatmap max %s at syn,vcn bins %s" % (
                hmax, 
                map(
                    lambda p: (
                        math.exp(p[0] * math.log(MAX_SYN) / IMAGE_SIZE), 
                        math.exp(p[1] * math.log(MAX_SYN) / IMAGE_SIZE)
                    ), 
                    max_bins
                )
            )
                
            syn_vals = list(syn_vals)
            vcn_vals = list(vcn_vals)
            syn_vals.sort()
            vcn_vals.sort()
            try:
                print "median syn %s vcn %s" % (syn_vals[len(syn_vals)/2], vcn_vals[len(vcn_vals)/2])
            except:
                pass

            print "histo: %s" % histo

        # render all centroids
        render(self.syn_values, self.vcn_values, 0)

        # render matching centroids
        syn_values, vcn_values, centroids = reanalyze()
        render(syn_values, vcn_values, 1)

        #heatmap = np.log(heatmap)
        #heatmap = 255.0 * heatmap / heatmap.max()
        
        heatmap = heatmap * HM_GAIN
        heatmap = heatmap * (heatmap <= 255.0) + 255.0 * (heatmap > 255.0)

        tifffile.imsave('heatmap.tiff', heatmap.astype(np.uint8))
        
    
    @adjust_level('u_floorlvl', 'floorlvl', trace="feature threshold set to %(level).5f")
    def adjust_floor_level(self, event):
        """Increase ('F') or decrease ('f') small feature threshold level."""
        pass
    
    @adjust_level('u_nuclvl', 'nuclvl', trace="negative vicinity threshold set to %(level).5f")
    def adjust_nuc_level(self, event):
        """Increase ('N') or decrease ('n') nuclei-scale feature threshold level."""
        pass

    @adjust_level('u_msklvl', 'msklvl', trace="red mask level set to %(level).5f")
    def adjust_msk_level(self, event):
        """Increase ('M') or descrease ('m') red-channel mask threshold level."""
        pass

    @adjust_level('u_zerlvl', 'zerlvl', trace="zero-crossing level set to %(level).5f")
    def adjust_zer_level(self, event):
        """Increase ('T') or decrease ('t') transparency zero-crossing level."""
        pass

    @adjust_level('u_toplvl', 'toplvl', trace="upper clipping level set to %(level).5f")
    def adjust_top_level(self, event):
        """Increase ('U') or decrease ('u') upper clipping level."""
        pass

    @adjust_level('u_transp', 'transp', 0.005, trace="opacity factor set to %(level).5f", tracenorm=False)
    def adjust_transp_level(self, event):
        """Increase ('O') or decrease ('o') opacity factor."""
        pass

