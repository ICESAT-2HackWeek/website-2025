# ---
# jupyter:
#   jupytext:
#     formats: py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.16.4
#   kernelspec:
#     display_name: Python 3 (ipykernel)
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Machine Learning with ICESat-2 data
#
# A machine learning pipeline from point clouds to photon classifications.
#
# Reimplementation of
# https://github.com/YoungHyunKoo/IS2_ML/blob/main/01_find_overlapped_data.ipynb

# %% [markdown]
# ```{admonition} Learning Objectives
# By the end of this tutorial, you should be able to:
# - Convert ICESat-2 point cloud data into an analysis/ML-ready format
# - Recognize the different levels of complexity of ML approaches and the
#   benefits/challenges of each
# - Learn the potential of using ML for ICESat-2 photon classification
# ```

# %% [markdown]
# ## Part 0: Setup

# %%
import earthaccess
import geopandas as gpd
import h5py
import numpy as np
import pandas as pd
import pygmt
import pystac_client
import rioxarray  # noqa: F401
import shapely.geometry
import stackstac
import torch
import tqdm

# %% [markdown]
# ## Part 1: Convert ICESat-2 data into ML-ready format
#
# Steps:
# - Get ATL07 data using [earthaccess](https://earthaccess.readthedocs.io)
# - Find coincident Sentinel-2 imagery by searching over a
#   [STAC API](https://pystac-client.readthedocs.io/en/v0.8.3/usage.html#itemsearch)
# - Filter to only strong beams, and 6 data variables
#
# TODO: copy Table 1 from Koo et al., 2023 paper

# %% [markdown]
# ### Search for ATL07 data over study area
#
# In this sub-section, we'll set up a spatiotemporal query to look for ICESat-2 ATL07
# sea ice data over the Ross Sea region around late October 2019.

# %%
# Authenticate using NASA EarthData login
auth = earthaccess.login()
s3 = earthaccess.get_s3fs_session(daac="NSIDC")  # Start an AWS S3 session

# %%
# Set up spatiotemporal query for ATL07 sea ice product
granules = earthaccess.search_data(
    short_name="ATL07",
    cloud_hosted=True,
    bounding_box=(-180, -78, -140, -70),  # xmin, ymin, xmax, ymax
    temporal=("2019-10-31", "2019-11-01"),
    version="006",
)
granules[-1]  # visualize last data granule

# %% [markdown]
# #### Find coincident Sentinel-2 imagery
#
# Let's also find some optical satellite images that were captured at around the same
# time and location as the ICESat-2 ATL07 tracks. We will be using
# [`pystac_client.Client.search`](https://pystac-client.readthedocs.io/en/v0.8.3/api.html#pystac_client.Client.search)
# and doing the search with two steps:
#
# 1. (Fast) search using date to find potential Sentinel-2/ICESat-2 pairs
# 2. (Slow) search using spatial intersection to ensure Sentinel-2 image overlaps with
#    ICESat-2 track.

# %%
# Connect to STAC API that hosts Sentinel-2 imagery on AWS us-west-2
catalog = pystac_client.Client.open(url="https://earth-search.aws.element84.com/v1")

# %%
# Loop over each ATL07 data granule, and find Sentinel-2 images from same time range
for granule in tqdm.tqdm(iterable=granules):
    # Get ATL07 time range from Unified Metadata Model (UMM)
    date_range = granule["umm"]["TemporalExtent"]["RangeDateTime"]
    start_time = date_range["BeginningDateTime"]  # e.g. '2019-02-24T19:51:47.580Z'
    end_time = date_range["EndingDateTime"]  # e.g. '2019-02-24T19:52:08.298Z'

    # 1st check (temporal match)
    search1 = catalog.search(
        collections="sentinel-2-l2a",  # Bottom-of-Atmosphere product
        bbox=[-180, -78, -140, -70],  # xmin, ymin, xmax, ymax
        datetime=f"{start_time}/{end_time}",
        query={"eo:cloud_cover": {"lt": 30}},  # max cloud cover
        max_items=10,
    )
    _item_collection = search1.item_collection()
    if (_item_len := len(_item_collection)) >= 1:
        # print(f"Potential: {_item_len} Sentinel-2 x ATL07 matches!")

        # 2nd check (spatial match) using centre-line track intersection
        file_obj = earthaccess.open(granules=[granule])[0]
        atl_file = h5py.File(name=file_obj, mode="r")
        linetrack = shapely.geometry.LineString(
            coordinates=zip(
                atl_file["gt2r/sea_ice_segments/longitude"][:10000],
                atl_file["gt2r/sea_ice_segments/latitude"][:10000],
            )
        ).simplify(tolerance=10)
        search2 = catalog.search(
            collections="sentinel-2-l2a",
            intersects=linetrack,
            datetime=f"{start_time}/{end_time}",
            max_items=10,
        )
        item_collection = search2.item_collection()
        if (item_len := len(item_collection)) >= 1:
            print(
                f"Found: {item_len} Sentinel-2 items coincident with granule:\n{granule}"
            )
            break  # uncomment this line if you want to find more matches

# %% [markdown]
# We should have found a match! In case you missed it, these are the two variables pointing to the data we'll use later:
#
# - `granule` - ICESat-2 ATL07 sea ice point cloud data
# - `item_collection` - Sentinel-2 optical satellite images

# %%

# %% [markdown]
# ### Filter to strong beams and required data variables
#
# Here, we'll open one ATL07 sea ice data file, and do some pre-processing.

# %%
# %%time
file_obj = earthaccess.open(granules=[granule])[0]
atl_file = h5py.File(name=file_obj, mode="r")
atl_file.keys()

# %% [markdown]
# Strong beams can be chosen based on the `sc_orient` variable.
#
# Ref: https://github.com/ICESAT-2HackWeek/strong-beams

# %%
# orientation - 0: backward, 1: forward, 2: transition
orient = atl_file["orbit_info"]["sc_orient"][:]
if orient == 0:
    strong_beams = ["gt1l", "gt2l", "gt3l"]
elif orient == 1:
    strong_beams = ["gt3r", "gt2r", "gt1r"]
strong_beams

# %%
for beam in strong_beams:
    print(beam)

# %% [markdown]
# Key data variables to use (for model training):
#   1. `photon_rate`: photon rate
#   2. `hist_w`: width of the photon height distribution
#   3. `background_r_norm`: background photon rate
#   4. `height_segment_height`: relative surface height
#   5. `height_segment_n_pulse_seg`: number of laser pulses
#   6. `hist_mean_h` - `hist_median_h`: difference between mean and median height
#
# Other data variables:
# - `x_atc` - Along track distance from the equator
# - `layer_flag` - Consolidated cloud flag { 0: 'likely_clear', 1: 'likely_cloudy' }
# - `height_segment_ssh_flag` - Sea surface flag { 0: 'sea ice', 1: 'sea surface' }
#
# Data dictionary at:
# https://nsidc.org/sites/default/files/documents/technical-reference/icesat2_atl07_data_dict_v006.pdf

# %%
gdf = gpd.GeoDataFrame(
    data={
        # Key data variables
        "photon_rate": atl_file[f"{beam}/sea_ice_segments/stats/photon_rate"][:],
        "hist_w": atl_file[f"{beam}/sea_ice_segments/stats/hist_w"][:],
        "background_r_norm": atl_file[
            f"{beam}/sea_ice_segments/stats/background_r_norm"
        ][:],
        "height_segment_height": atl_file[
            f"{beam}/sea_ice_segments/heights/height_segment_height"
        ][:],
        "height_segment_n_pulse_seg": atl_file[
            f"{beam}/sea_ice_segments/heights/height_segment_n_pulse_seg"
        ][:],
        "hist_mean_h": atl_file[f"{beam}/sea_ice_segments/stats/hist_mean_h"][:],
        "hist_median_h": atl_file[f"{beam}/sea_ice_segments/stats/hist_median_h"][:],
        # Other data variables
        "x_atc": atl_file[f"{beam}/sea_ice_segments/seg_dist_x"][:],
        "layer_flag": atl_file[f"{beam}/sea_ice_segments/stats/layer_flag"][:],
        "height_segment_ssh_flag": atl_file[
            f"{beam}/sea_ice_segments/heights/height_segment_ssh_flag"
        ][:],
    },
    geometry=gpd.points_from_xy(
        x=atl_file[f"{beam}/sea_ice_segments/longitude"][:],
        y=atl_file[f"{beam}/sea_ice_segments/latitude"][:],
    ),
    crs="OGC:CRS84",
)

# %%
gdf = gdf[gdf.layer_flag == 0].reset_index(drop=True)  # keep non-cloudy points only
print(f"Total number of rows: {len(gdf)}")

# %%
gdf

# %%

# %% [markdown]
# ### Optical imagery to label point clouds
#
# Let's use the Sentinel-2 satellite image we found to label each ATL07 photon. We'll
# make a new column called `sea_ice_label` that can have either of these classifications:
#
# 0. thick/snow-covered sea ice
# 1. thin/gray sea ice
# 2. open water
#
# These labels will be empirically determined based on the brightness (or surface
# reflectance) of the Sentinel-2 pixel.
#
# ```{note}
# Sea ice can move very quickly in minutes, so while we've tried our best to find a
# Sentinel-2 image that was captured at about the same time as the ICESat-2 track, it is
# very likely that we will still be misclassifying some of the points below because the
# image and point clouds are not perfectly aligned.
# ```

# %%
# Get first STAC item from collection
item = item_collection.items[0]
item


# %%
# Get RGB bands from Sentinel-2 STAC item as an xarray.DataArray
da_image = stackstac.stack(
    items=item,
    assets=["red", "green", "blue"],
    resolution=60,
    rescale=False,  # keep original 14-bit scale
    dtype=np.float32,
    fill_value=np.float32(np.nan),
)
da_image = da_image.squeeze(dim="time")  # drop time dimension so only (band, y, x) left
da_image


# %%
# Get bounding box of Sentinel-2 image to spatially subset ATL07 geodataframe
bbox = shapely.geometry.box(*da_image.rio.bounds())  # xmin, ymin, xmax, ymax
gdf = gdf.to_crs(crs=da_image.crs)  # reproject point cloud to Sentinel-2 image's CRS
gdf = gdf[gdf.within(other=bbox)].reset_index()  # subset point cloud to Sentinel-2 bbox

# %% [markdown]
# Let's plot the Sentinel-2 satellite image and ATL07 points! The ATL07 data has a
# `height_segment_ssh_flag` variable with a binary sea ice (0) / sea surface (1)
# classification, which we'll plot using two different colors to check that things
# overlap more or less.

# %%
fig = pygmt.Figure()

# Plot Sentinel-2 RGB image
# Convert from 14-bit to 8-bit color scale for PyGMT
fig.grdimage(grid=((da_image / 2**14) * 2**8).astype(np.uint8), frame=True)

# Plot ATL07 points
# Sea ice points in blue
df_ice = gdf[gdf.height_segment_ssh_flag == 0].get_coordinates()
fig.plot(x=df_ice.x, y=df_ice.y, style="c0.2c", fill="blue", label="Sea ice")
# Sea surface points in orange
df_water = gdf[gdf.height_segment_ssh_flag == 1].get_coordinates()
fig.plot(x=df_water.x, y=df_water.y, style="c0.2c", fill="orange", label="Sea surface")
fig.legend()
fig.show()


# %% [markdown]
# Looking good! Notice how the orange (sea surface) coincide with the cracks in the sea
# ice.


# %% [markdown]
# Next, we'll want to do better than a binary 0/1 or ice/water classification, and pick
# up different shades of gray (white thick ice, gray thin ice, black water). Let's
# use the X and Y coordinates from the point cloud to pick up surface reflectance values
# from the Sentinel-2 image.
#
# PyGMT's [`grdtrack`](https://www.pygmt.org/v0.12.0/api/generated/pygmt.grdtrack.html)
# function can be used to do this X/Y sampling from a 1-band grid.

# %%
df_red = pygmt.grdtrack(
    grid=da_image.sel(band="red").compute(),  # Choose only the Red band
    points=gdf.get_coordinates(),  # x/y coordinates from ATL07
    newcolname="red_band_value",
    interpolation="n",  # nearest neighbour
)

# %%
# Plot cross-section
df_red.plot.scatter(x="y", y="red_band_value", title="Red band values in y-direction")

# %% [markdown]
# The cross-section view shows most points having a Red band reflectance value of 10000,
# that should correspond to white sea ice. Darker values near 0 would be water, and
# intermediate values around 6000 would be thin ice.
#
# (Click 'Show code cell content' below if you'd like to see the histogram plot)

# %% editable=true slideshow={"slide_type": ""} tags=["hide-cell"]
df_red.hist(column="red_band_value", bins=30)

# %% [markdown]
# To keep things simple, we'll label the `surface_type` of each ATL07 point
# using a simple threshold:
#
# | Int label |      Surface Type     |      Bin values     |
# |-----------|:---------------------:|:-------------------:|
# |     0     | Water (dark)          | `0 <= x <= 4000`    |
# |     1     | Thin sea ice (gray)   | `4000 < x <= 8000`  |
# |     2     | Thick sea ice (white) | `8000 < x <= 14000` |


# %%
gdf["surface_type"] = pd.cut(
    x=df_red["red_band_value"],
    bins=[0, 4000, 8000, 14000],
    labels=[0, 1, 2],  # "water", "thin_ice", "thick_ice"
)

# %% [markdown]
# There are some NaN values in some rows of our geodataframe (which had no matching
# Sentinel-2 pixel value) that should be dropped here.

# %%
gdf = gdf.dropna().reset_index(drop=True)

# %%
gdf

# %% [markdown]
# ### Save to GeoParquet

# %% [markdown]
# Let's save the ATL07 photon data to a GeoParquet file so we don't have to run all the
# pre-processing code above again.

# %%
gdf.to_parquet(
    path="ATL07_photons.gpq", compression="zstd", schema_version="1.0.0-beta.1"
)

# %% [markdown]
# ```{note} To compress or not?
# When storing your data, note that there is a tradeoff in terms of compression and read
# speeds. Uncompressed data would typically be fastest to read (assuming no network
# transfer) but result in large file sizes. We'll choose Zstandard (zstd) as the
# compression method here as it is typically faster to read (compared to the default
# 'snappy' compression codec), and still compresses well into a small file size.
# ```

# %%
# Load GeoParquet file back into geopandas.GeoDataFrame
gdf = gpd.read_parquet(path="ATL07_photons.gpq")

# %%

# %% [markdown]
# ## Part 2: Choosing a Machine Learning algorithm

# %% [markdown]
# ### Moving data from CPU to GPU
#
# Machine learning models are compute intensive, and typically run on specialized
# hardware called Graphical Processing Units (GPUs) instead of ordinary CPUs. Depending
# on your input data format (images, tables, audio, etc), and the machine learning
# library/framework you'll use (e.g. Pytorch, Tensorflow, RAPIDS AI CuML, etc), there
# will be different ways to transfer data from disk storage -> CPU -> GPU.
#
# For this exercise, we'll be using [PyTorch](https://pytorch.org), and do the following
# data conversions:
#
# [`geopandas.GeoDataFrame`](https://geopandas.org/en/v1.0.0/docs/reference/api/geopandas.GeoDataFrame.html) ->
# [`pandas.DataFrame`](https://pandas.pydata.org/pandas-docs/version/2.2/reference/api/pandas.DataFrame.html) ->
# [`torch.Tensor`](https://pytorch.org/docs/2.4/tensors.html#torch.Tensor) ->
# [torch `Dataset`](https://pytorch.org/docs/2.4/data.html#torch.utils.data.Dataset) ->
# [torch `DataLoader`](https://pytorch.org/docs/2.4/data.html#torch.utils.data.DataLoader)

# %%
# Select data variables from DataFrame that will be used for training
df = gdf[
    [
        "photon_rate",
        "hist_w",
        "background_r_norm",
        "height_segment_height",
        "height_segment_n_pulse_seg",
        "hist_mean_h",
        "hist_median_h",
    ]
]
tensor = torch.tensor(data=df.values)  # convert pandas.DataFrame to torch.Tensor
assert tensor.shape == torch.Size([38246, 7])  # (rows, columns)
dataset = torch.utils.data.TensorDataset(tensor)  # turn torch.Tensor into torch Dataset
dataloader = torch.utils.data.DataLoader(  # put torch Dataset in a DataLoader
    dataset=dataset,
    batch_size=128,  # mini-batch size
    shuffle=True,
)

# %% [markdown]
# PyTorch's [`DataLoader`](https://pytorch.org/docs/2.4/data.html#torch.utils.data.DataLoader)
# is a convenient container to hold tensor data, and makes it easy for us to iterate
# over mini-batches using a for-loop.)

# %%
for batch in dataloader:
    minibatch: torch.Tensor = batch[0]
    assert minibatch.shape == (128, 7)
    assert minibatch.device == torch.device("cpu")  # Data is on CPU

    minibatch = minibatch.to(device="cuda")  # Move data to GPU
    assert minibatch.device == torch.device("cuda:0")  # Data is on GPU now!

    break

# %%

# %% [markdown]
# ## Part 3: Training the neural network
#
# Use multi-layer perceptron with:
# - 2 hidden layers, 50 nodes each
# - tanh activation function
# - final layer with 3 nodes, for 3 surface types (open water, thin ice, thick/snow-covered ice)
# - Adam optimizer

# %%

# %%

# %% [markdown]
# ## References
# - Koo, Y., Xie, H., Kurtz, N. T., Ackley, S. F., & Wang, W. (2023).
#   Sea ice surface type classification of ICESat-2 ATL07 data by using data-driven
#   machine learning model: Ross Sea, Antarctic as an example. Remote Sensing of
#   Environment, 296, 113726. https://doi.org/10.1016/j.rse.2023.113726


# %%
