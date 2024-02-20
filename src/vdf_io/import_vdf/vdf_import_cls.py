import datetime
import json
import os
import numpy as np
from packaging.version import Version
import abc
from tqdm import tqdm

from qdrant_client.http.models import Distance

import vdf_io
from vdf_io.constants import ID_COLUMN
from vdf_io.meta_types import NamespaceMeta, VDFMeta
from vdf_io.util import (
    expand_shorthand_path,
    get_final_data_path,
    get_parquet_files,
    read_parquet_progress,
)


class ImportVDB(abc.ABC):
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not hasattr(cls, "DB_NAME_SLUG"):
            raise TypeError(
                f"Class {cls.__name__} lacks required class variable 'DB_NAME_SLUG'"
            )

    def __init__(self, args):
        self.args = args
        if self.args.get("hf_dataset", None) is None:
            self.args["dir"] = expand_shorthand_path(self.args["dir"])
            if not os.path.isdir(self.args["dir"]):
                raise Exception("Invalid dir path")
            if not os.path.isfile(os.path.join(self.args["dir"], "VDF_META.json")):
                raise Exception("Invalid dir path, VDF_META.json not found")
            # Check if the VDF_META.json file exists
            vdf_meta_path = os.path.join(self.args["dir"], "VDF_META.json")
            if not os.path.isfile(vdf_meta_path):
                raise Exception("VDF_META.json not found in the specified directory")
            with open(vdf_meta_path) as f:
                self.vdf_meta = json.load(f)
        else:
            hf_dataset = self.args.get("hf_dataset", None)
            index_name = hf_dataset.split("/")[-1]
            from huggingface_hub import HfFileSystem

            fs = HfFileSystem()
            hf_files = fs.ls(f"datasets/{hf_dataset}", detail=False)
            if f"datasets/{hf_dataset}/VDF_META.json" in hf_files:
                print(f"Found VDF_META.json in {hf_dataset} on HuggingFace Hub")
                self.vdf_meta = json.loads(
                    fs.read_text(f"datasets/{hf_dataset}/VDF_META.json")
                )
            else:
                self.vdf_meta = VDFMeta(
                    version=vdf_io.__version__,
                    file_structure=[],
                    author=hf_dataset.split("/")[0],
                    exported_from="hf",
                    indexes={
                        index_name: [
                            NamespaceMeta(
                                namespace="",
                                index_name=index_name,
                                total_vector_count=self.args.get("max_num_rows"),
                                exported_vector_count=self.args.get("max_num_rows"),
                                dimensions=self.args.get("vector_dim", -1),
                                model_name=self.args.get("model_name", ""),
                                vector_columns=self.args.get(
                                    "vector_columns", "vector"
                                ).split(","),
                                data_path=".",
                                metric=self.args.get("metric", Distance.COSINE),
                            )
                        ]
                    },
                    exported_at=datetime.datetime.now().astimezone().isoformat(),
                    id_column=self.args.get("id_column", ID_COLUMN),
                ).dict()
            print(json.dumps(self.vdf_meta, indent=4))
        self.id_column = self.vdf_meta.get("id_column", ID_COLUMN)
        if "indexes" not in self.vdf_meta:
            raise Exception("Invalid VDF_META.json, 'indexes' key not found")
        if "version" not in self.vdf_meta:
            print("Warning: 'version' key not found in VDF_META.json")
        elif "library_version" not in self.args:
            print(
                "Warning: 'library_version' not found in args. Skipping version check."
            )
        elif Version(self.vdf_meta["version"]) > Version(self.args["library_version"]):
            print(
                f"Warning: The version of vector-io library: ({self.args['library_version']}) is behind the version of the vdf directory: ({self.vdf_meta['version']})."
            )
            print(
                "Please upgrade the vector-io library to the latest version to ensure compatibility."
            )

    @abc.abstractmethod
    def upsert_data():
        """
        Get data from vector database
        """
        raise NotImplementedError

    def get_vector_column_name(self, index_name, namespace_meta):
        if "vector_columns" not in namespace_meta:
            print(
                "vector_columns not found in namespace metadata. Using 'vector' as the vector column name."
            )
            vector_column_name = "vector"
            vector_column_names = [vector_column_name]
        else:
            vector_column_names = namespace_meta["vector_columns"]
            vector_column_name = vector_column_names[0]
            if len(vector_column_names) > 1:
                tqdm.write(
                    f"Warning: More than one vector column found for index {index_name}."
                    f" Only the first vector column {vector_column_name} will be imported."
                )
        return vector_column_names, vector_column_name

    def get_parquet_files(self, data_path):
        return get_parquet_files(data_path, self.args)

    def get_final_data_path(self, data_path):
        return get_final_data_path(
            self.args["cwd"], self.args["dir"], data_path, self.args
        )

    def get_file_path(self, final_data_path, parquet_file):
        if self.args.get("hf_dataset", None):
            return parquet_file
        return os.path.join(final_data_path, parquet_file)

    def resolve_dims(self, namespace_meta, new_collection_name):
        final_data_path = self.get_final_data_path(namespace_meta["data_path"])
        parquet_files = self.get_parquet_files(final_data_path)
        _, vector_column_name = self.get_vector_column_name(
            new_collection_name, namespace_meta
        )
        dims = -1
        for file in tqdm(parquet_files, desc="Iterating parquet files"):
            file_path = self.get_file_path(final_data_path, file)
            df = read_parquet_progress(file_path)
            first_el = df[vector_column_name].iloc[0]
            if isinstance(first_el, list) and len(first_el) > 1:
                return len(first_el)
            if isinstance(first_el, np.ndarray):
                if first_el.shape[0] > 1:
                    return first_el.shape[0]
                if first_el.shape[0] == 1:
                    return first_el[0].shape[0]
        return dims
