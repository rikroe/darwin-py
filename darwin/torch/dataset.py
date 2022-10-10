from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
import torchvision.transforms as T
import torchvision.transforms.functional as TF
from darwin.cli_functions import _error, _load_client
from darwin.dataset import LocalDataset
from darwin.dataset.identifier import DatasetIdentifier
from darwin.dataset.utils import convert_to_rgb
from darwin.torch.transforms import (
    Compose,
    ConvertPolygonsToInstanceMasks,
    ConvertPolygonsToSemanticMask,
)
from darwin.torch.utils import polygon_area
from darwin.utils import convert_polygons_to_sequences
from PIL import Image as PILImage

import torch
from torch.functional import Tensor


def get_dataset(
    dataset_slug: str,
    dataset_type: str,
    partition: Optional[str] = None,
    split: str = "default",
    split_type: str = "random",
    transform: Optional[List] = None,
) -> LocalDataset:
    """
    Creates and returns a ``LocalDataset``.

    Parameters
    ----------
    dataset_slug : str
        Slug of the dataset to retrieve.
    dataset_type : str
        The type of dataset ``["classification", "instance-segmentation", "object-detection", "semantic-segmentation"]``.
    partition : str, default: None
        Selects one of the partitions ``["train", "val", "test", None]``.
    split : str, default: "default"
        Selects the split that defines the percentages used.
    split_type : str, default: "random"
        Heuristic used to do the split ``[random, stratified]``.
    transform : Optional[List], default: None
        List of PyTorch transforms.
    """
    dataset_functions = {
        "classification": ClassificationDataset,
        "instance-segmentation": InstanceSegmentationDataset,
        "semantic-segmentation": SemanticSegmentationDataset,
        "object-detection": ObjectDetectionDataset,
    }
    dataset_function = dataset_functions.get(dataset_type)
    if not dataset_function:
        list_of_types = ", ".join(dataset_functions.keys())
        return _error(f"dataset_type needs to be one of '{list_of_types}'")

    identifier = DatasetIdentifier.parse(dataset_slug)
    client = _load_client(offline=True)

    for p in client.list_local_datasets(team_slug=identifier.team_slug):
        if identifier.dataset_slug == p.name:
            return dataset_function(
                dataset_path=p,
                partition=partition,
                split=split,
                split_type=split_type,
                release_name=identifier.version,
                transform=transform,
            )

    _error(
        f"Dataset '{identifier.dataset_slug}' does not exist locally. "
        f"Use 'darwin dataset remote' to see all the available datasets, "
        f"and 'darwin dataset pull' to pull them."
    )


class ClassificationDataset(LocalDataset):
    """
    Represents a LocalDataset used for training on classification tasks.

    Attributes
    ----------
    transform : Optional[Callable], default: None
        torchvision transform function to run on the dataset.
    is_multi_label : bool, default: False
        Whether the dataset is multilabel or not.

    Parameters
    ----------
    transform: Optional[Union[Callable, List[Callable]]], default: None
        torchvision function or list to set the ``transform`` attribute. If it is a list, it will
        be composed via torchvision.
    """

    def __init__(self, transform: Optional[Union[Callable, List]] = None, **kwargs):
        super().__init__(annotation_type="tag", **kwargs)

        if transform is not None and isinstance(transform, list):
            transform = Compose(transform)

        self.transform: Optional[Callable] = transform

        self.is_multi_label = False
        self.check_if_multi_label()

    def __getitem__(self, index: int) -> Tuple[Tensor, Tensor]:
        """
        See superclass for documentation.

        Parameters
        ----------
        index : int
            The index of the image.

        Returns
        -------
        Tuple[Tensor, Tensor]
            A tuple of tensors, where the first value is the image tensor and the second is the
            target's tensor.
        """
        img: PILImage.Image = self.get_image(index)
        if self.transform is not None:
            img_tensor = self.transform(img)
        else:
            img_tensor = TF.to_tensor(img)

        target = self.get_target(index)

        return img_tensor, target

    def get_target(self, index: int) -> Tensor:
        """
        Returns the classification target.

        Parameters
        ----------
        index : int
            Index of the image.

        Returns
        -------
        Tensor
            The target's tensor.
        """

        data = self.parse_json(index)
        annotations = data.pop("annotations")
        tags = [a["name"] for a in annotations if "tag" in a]

        assert len(tags) >= 1, f"No tags were found for index={index}"

        target: Tensor = torch.tensor(self.classes.index(tags[0]))

        if self.is_multi_label:
            target = torch.zeros(len(self.classes))
            # one hot encode all the targets
            for tag in tags:
                idx = self.classes.index(tag)
                target[idx] = 1

        return target

    def check_if_multi_label(self) -> None:
        """
        Loops over all the ``.json`` files and checks if we have more than one tag in at least one
        file, if yes we assume the dataset is for multi label classification.
        """
        for idx in range(len(self)):
            target = self.parse_json(idx)
            annotations = target.pop("annotations")
            tags = [a["name"] for a in annotations if "tag" in a]

            if len(tags) > 1:
                self.is_multi_label = True
                break

    def get_class_idx(self, index: int) -> int:
        """
        Returns the ``category_id`` of the image with the given index.

        Parameters
        ----------
        index : int
            Index of the image.

        Returns
        -------
        int
            ``category_id`` of the image.
        """
        target: Tensor = self.get_target(index)
        return target["category_id"]

    def measure_weights(self) -> np.ndarray:
        """
        Computes the class balancing weights (not the frequencies!!) given the train loader.
        Gets the weights proportional to the inverse of their class frequencies.
        The vector sums up to 1.

        Returns
        -------
        np.ndarray[float]
            Weight for each class in the train set (one for each class) as a 1D array normalized.
        """
        # Collect all the labels by iterating over the whole dataset
        labels = []
        for i, _filename in enumerate(self.images_path):
            target: Tensor = self.get_target(i)
            if self.is_multi_label:
                # get the indices of the class present
                target = torch.where(target == 1)[0]
                labels.extend(target.tolist())
            else:
                labels.append(target.item())

        return self._compute_weights(labels)


class InstanceSegmentationDataset(LocalDataset):
    """
    Represents an instance of a LocalDataset used for training on instance segmentation tasks.

    Parameters
    ----------
    transform: Optional[Union[Callable, List[Callable]]], default: None
        torchvision function or list to set the ``transform`` attribute. If it is a list, it will
        be composed via torchvision.

    Attributes
    ----------
    transform : Optional[Callable], default: None
        torchvision transform function to run on the dataset.
    is_multi_label : bool, default: False
        Whether the dataset is multilabel or not.
    convert_polygons : ConvertPolygonsToInstanceMasks
        Object used to convert polygons to instance masks.

    """

    def __init__(self, transform: Optional[Union[Callable, List]] = None, **kwargs):
        super().__init__(annotation_type="polygon", **kwargs)

        if transform is not None and isinstance(transform, list):
            transform = Compose(transform)

        self.transform: Optional[Callable] = transform

        self.convert_polygons = ConvertPolygonsToInstanceMasks()

    def __getitem__(self, index: int) -> Tuple[Tensor, Dict[str, Any]]:
        """
        Notes
        -----
        The return value is a dict with the following fields:
            image_id : int
                Index of the image inside the dataset
            image_path: str
                The path to the image on the file system
            labels : tensor(n)
                The class label of each one of the instances
            masks : tensor(n, H, W)
                Segmentation mask of each one of the instances
            boxes : tensor(n, 4)
                Coordinates of the bounding box enclosing the instances as [x, y, x, y]
            area : float
                Area in pixels of each one of the instances
        """
        img: PILImage.Image = self.get_image(index)
        target: Dict[str, Any] = self.get_target(index)

        img, target = self.convert_polygons(img, target)
        if self.transform is not None:
            img_tensor, target = self.transform(img, target)
        else:
            img_tensor = TF.to_tensor(img)

        return img_tensor, target

    def get_target(self, index: int) -> Dict[str, Any]:
        """
        Builds and returns the target dictionary for the item at the given index.
        The target dictionary will have the following format:

        .. code-block:: python

            {
                "annotations": [
                    {
                        "category_id": int,
                        "segmentation": List[List[int | float]],
                        "bbox": List[float],
                        "area": float
                    }
                ]
            }

        Parameters
        ----------
        index : int
            The actual index of the item in the ``Dataset``.

        Returns
        -------
        Dict[str, Any]
            The target.
        """
        target = self.parse_json(index)

        annotations = []
        for annotation in target["annotations"]:
            if "polygon" not in annotation and "complex_polygon" not in annotation:
                print(f"Warning: missing polygon in annotation {self.annotations_path[index]}")
            # Extract the sequences of coordinates from the polygon annotation
            annotation_type: str = "polygon" if "polygon" in annotation else "complex_polygon"
            sequences = convert_polygons_to_sequences(
                annotation[annotation_type]["path"],
                height=target["height"],
                width=target["width"],
            )
            # Compute the bbox of the polygon
            x_coords = [s[0::2] for s in sequences]
            y_coords = [s[1::2] for s in sequences]
            min_x: float = np.min([np.min(x_coord) for x_coord in x_coords])
            min_y: float = np.min([np.min(y_coord) for y_coord in y_coords])
            max_x: float = np.max([np.max(x_coord) for x_coord in x_coords])
            max_y: float = np.max([np.max(y_coord) for y_coord in y_coords])
            w: float = max_x - min_x + 1
            h: float = max_y - min_y + 1
            # Compute the area of the polygon
            # TODO fix with addictive/subtractive paths in complex polygons
            poly_area: float = np.sum([polygon_area(x_coord, y_coord) for x_coord, y_coord in zip(x_coords, y_coords)])

            # Create and append the new entry for this annotation
            annotations.append(
                {
                    "category_id": self.classes.index(annotation["name"]),
                    "segmentation": sequences,
                    "bbox": [min_x, min_y, w, h],
                    "area": poly_area,
                }
            )
        target["annotations"] = annotations

        return target

    def measure_weights(self) -> np.ndarray:
        """
        Computes the class balancing weights (not the frequencies!!) given the train loader
        Get the weights proportional to the inverse of their class frequencies.
        The vector sums up to 1.

        Returns
        -------
        class_weights : np.ndarray[float]
            Weight for each class in the train set (one for each class) as a 1D array normalized.
        """
        # Collect all the labels by iterating over the whole dataset
        labels: List[int] = []
        for i, _ in enumerate(self.images_path):
            target = self.get_target(i)
            labels.extend([a["category_id"] for a in target["annotations"]])
        return self._compute_weights(labels)


class SemanticSegmentationDataset(LocalDataset):
    """
    Represents an instance of a LocalDataset used for training on semantic segmentation tasks.

    Parameters
    ----------
    transform : Optional[Union[List[Callable], Callable]], default: None
        torchvision function or list to set the ``transform`` attribute. If it is a list, it will
        be composed via torchvision.

    Attributes
    ----------
    transform : Optional[Callable], default: None
        torchvision transform function(s) to run on the dataset.
    convert_polygons : ConvertPolygonsToSemanticMask
        Object used to convert polygons to semantic masks.
    """

    def __init__(self, transform: Optional[Union[List[Callable], Callable]] = None, **kwargs):

        super().__init__(annotation_type="polygon", **kwargs)

        if transform is not None and isinstance(transform, list):
            transform = Compose(transform)

        self.transform: Optional[Callable] = transform
        self.convert_polygons = ConvertPolygonsToSemanticMask()

    def __getitem__(self, index: int) -> Tuple[Tensor, Dict[str, Any]]:
        """
        See superclass for documentation

        Notes
        -----
        The return value is a dict with the following fields:
            image_id : int
                Index of the image inside the dataset
            image_path: str
                The path to the image on the file system
            mask : tensor(H, W)
                Segmentation mask where each pixel encodes a class label
        """
        img: PILImage.Image = self.get_image(index)
        target: Dict[str, Any] = self.get_target(index)

        img, target = self.convert_polygons(img, target)
        if self.transform is not None:
            img_tensor, target = self.transform(img, target)
        else:
            img_tensor = TF.to_tensor(img)

        return img_tensor, target

    def get_target(self, index: int) -> Dict[str, Any]:
        """
        Builds and returns the target dictionary for the item at the given index.
        The returned dictionary has the following structure:

        .. code-block:: python

            {
                "annotations": [
                    {
                        "category_id": int,
                        "segmentation": List[List[float | int]]
                    }
                ]
            }

        Parameters
        ----------
        index : int
            The actual index of the item in the ``Dataset``.

        Returns
        -------
        Dict[str, Any]
            The target.
        """
        target = self.parse_json(index)

        annotations: List[Dict[str, Union[int, List[List[Union[int, float]]]]]] = []
        for obj in target["annotations"]:
            sequences = convert_polygons_to_sequences(
                obj["polygon"]["path"],
                height=target["height"],
                width=target["width"],
            )
            # Discard polygons with less than three points
            sequences[:] = [s for s in sequences if len(s) >= 6]
            if not sequences:
                continue
            annotations.append(
                {
                    "category_id": self.classes.index(obj["name"]),
                    "segmentation": sequences,
                }
            )
        target["annotations"] = annotations

        return target

    def measure_weights(self) -> np.ndarray:
        """
        Computes the class balancing weights (not the frequencies!!) given the train loader
        Get the weights proportional to the inverse of their class frequencies.
        The vector sums up to 1.

        Returns
        -------
        class_weights : np.ndarray[float]
            Weight for each class in the train set (one for each class) as a 1D array normalized.
        """
        # Collect all the labels by iterating over the whole dataset
        labels = []
        for i, _ in enumerate(self.images_path):
            target = self.get_target(i)
            labels.extend([a["category_id"] for a in target["annotations"]])
        return self._compute_weights(labels)


class ObjectDetectionDataset(LocalDataset):
    """
    Represents an instance of a LocalDataset used for training on object detection tasks.

    Parameters
    ----------
    transform : Optional[Union[List[Callable], Callable]], default: None
        torchvision function or list to set the ``transform`` attribute. If it is a list, it will
        be composed via torchvision.

    Attributes
    ----------
    transform : Optional[Callable], default: None
        torchvision transform function(s) to run on the dataset.
    """

    def __init__(self, transform: Optional[List] = None, **kwargs):
        super().__init__(annotation_type="bounding_box", **kwargs)

        if transform is not None and isinstance(transform, list):
            transform = Compose(transform)

        self.transform: Optional[Callable] = transform

    def __getitem__(self, index: int):
        """
        Notes
        -----
        The return value is a dict with the following fields:
            image_id : int
                Index of the image inside the dataset
            image_path: str
                The path to the image on the file system
            labels : tensor(n)
                The class label of each one of the instances
            boxes : tensor(n, 4)
                Coordinates of the bounding box enclosing the instances as [x, y, w, h] (coco format)
            area : float
                Area in pixels of each one of the instances
        """
        img: PILImage.Image = self.get_image(index)
        target: Dict[str, Any] = self.get_target(index)

        if self.transform is not None:
            img_tensor, target = self.transform(img, target)
        else:
            img_tensor = TF.to_tensor(img)

        return img_tensor, target

    def get_target(self, index: int) -> Dict[str, Tensor]:
        """
        Builds and returns the target dictionary for the item at the given index.
        The returned dictionary has the following structure:

        .. code-block:: python

            {
                "boxes": Tensor,
                "area": Tensor,
                "labels": Tensor,
                "image_id": Tensor,
                "iscrowd": Tensor
            }

        Parameters
        ----------
        index : int
            The actual index of the item in the ``Dataset``.

        Returns
        -------
        Dict[str, Any]
            The target.
        """
        target = self.parse_json(index)
        annotations = target.pop("annotations")

        targets = []
        for annotation in annotations:
            bbox = annotation["bounding_box"]

            x = bbox["x"]
            y = bbox["y"]
            w = bbox["w"]
            h = bbox["h"]

            bbox = torch.tensor([x, y, w, h])
            area = bbox[2] * bbox[3]
            label = torch.tensor(self.classes.index(annotation["name"]))

            ann = {"bbox": bbox, "area": area, "label": label}

            targets.append(ann)
        # following https://pytorch.org/tutorials/intermediate/torchvision_tutorial.html
        stacked_targets = {
            "boxes": torch.stack([v["bbox"] for v in targets]),
            "area": torch.stack([v["area"] for v in targets]),
            "labels": torch.stack([v["label"] for v in targets]),
            "image_id": torch.tensor([index]),
        }

        stacked_targets["iscrowd"] = torch.zeros_like(stacked_targets["labels"])

        return stacked_targets

    def measure_weights(self) -> np.ndarray:
        """
        Computes the class balancing weights (not the frequencies!!) given the train loader
        Get the weights proportional to the inverse of their class frequencies.
        The vector sums up to 1.

        Returns
        -------
        class_weights : np.ndarray[float]
            Weight for each class in the train set (one for each class) as a 1D array normalized.
        """
        # Collect all the labels by iterating over the whole dataset
        labels = []
        for i, _ in enumerate(self.images_path):
            target = self.get_target(i)
            labels.extend(target["labels"].tolist())
        return self._compute_weights(labels)


class HFClassificationDataset(ClassificationDataset):
    """
    Represents a LocalDataset used for training on classification tasks with HuggingFace.

    Attributes
    ----------
    transform : Optional[Callable], default: None
        torchvision transform function to run on the dataset.
    is_multi_label : bool, default: False
        Whether the dataset is multilabel or not.

    Parameters
    ----------
    transform: Optional[Union[Callable, List[Callable]]], default: None
        torchvision function or list to set the ``transform`` attribute. If it is a list, it will
        be composed via torchvision.
    """

    def __getitem__(self, index: int) -> Dict:
        """
        See superclass for documentation.

        Parameters
        ----------
        index : int
            The index of the image.

        Returns
        -------
        Dict
            TODO update
        """
        img: PILImage.Image = self.get_image(index)
        if self.transform is not None:
            img_tensor = self.transform(img)
        else:
            img_tensor = TF.to_tensor(img)

        target = self.get_target(index)

        return {"pixel_values": img_tensor, "label": target.item()}


class HFSemanticSegmentationDataset(SemanticSegmentationDataset):
    """
    Represents a LocalDataset used for training on semantic segmentation tasks with HuggingFace.

    Attributes
    ----------
    transform : Optional[Callable], default: None
        torchvision transform function to run on the dataset.
    is_multi_label : bool, default: False
        Whether the dataset is multilabel or not.

    Parameters
    ----------
    transform: Optional[Union[Callable, List[Callable]]], default: None
        torchvision function or list to set the ``transform`` attribute. If it is a list, it will
        be composed via torchvision.
    """

    def __getitem__(self, index: int) -> Tuple[Tensor, Dict[str, Any]]:
        """
        See superclass for documentation

        Notes
        -----
        The return value is a dict with the following fields:
            image_id : int
                Index of the image inside the dataset
            image_path: str
                The path to the image on the file system
            mask : tensor(H, W)
                Segmentation mask where each pixel encodes a class label
        """
        img: PILImage.Image = self.get_image(index)
        target: Dict[str, Any] = self.get_target(index)

        img, target = self.convert_polygons(img, target)
        img = convert_to_rgb(img)
        # Random crop
        width, height = img.size
        new_width, new_height = (512, int(height * 512 / width)) if height > width else (int(width * 512 / height), 512)
        transform = T.Compose(
            [
                T.Resize(size=(new_height, new_width)),
                T.CenterCrop(size=(512, 512)),
                T.ToTensor(),
            ]
        )
        img_tensor = transform(img)
        mask = torch.tensor(np.expand_dims(target["mask"], 0))
        resized_mask = TF.resize(mask, size=(new_height, new_width))
        target = TF.center_crop(resized_mask, output_size=(512, 512))
        target = torch.squeeze(target, 0).long()
        return {"pixel_values": img_tensor, "labels": target}


class HFObjectDetectionDataset(ObjectDetectionDataset):

    """
    Represents a LocalDataset used for training on object detection tasks with Huggingface.

    Parameters
    ----------
    transform : Optional[Union[List[Callable], Callable]], default: None
        torchvision function or list to set the ``transform`` attribute. If it is a list, it will
        be composed via torchvision.

    Attributes
    ----------
    transform : Optional[Callable], default: None
        torchvision transform function(s) to run on the dataset.
    """


    def __getitem__(self, index: int) -> Dict:
        """
        See superclass for documentation.

        Parameters
        ----------
        index : int
            The index of the image.

        Returns
        -------
        Dict
            TODO update
        """
        img: PILImage.Image = self.get_image(index)
        if self.transform is not None:
            img_tensor = self.transform(img)
        else:
            img_tensor = TF.to_tensor(img)

        target = self.get_target(index)

        return img_tensor, target



    def get_target(self, index: int) -> Dict[str, Any]:
        """
        Builds and returns the target dictionary for the item at the given index.
        The returned dictionary has the following structure:

        .. code-block python

        {
            "image_id": int
            "annotations":
                [
                    "image_id": 'str',
                    "id": int,
                    "area": int,
                    "bbox": [float, float, float, float],
                    "segmentation": [[float_x_1, float_y_2, ... , float_x_n, float_y_n]],
                    "iscrowd": Bool
                    "category_id": int
                ]
        }

        For more info, see the input of the Huggingface DetrFeatureExtractor:
        https://huggingface.co/docs/transformers/v4.22.2/en/model_doc/detr#transformers.DetrFeatureExtractor

        Parameters
        ----------
        index : int
            The actual index of the item in the ``Dataset``.

        Returns
        -------
        Dict[str, Any]
            The target.
        """
        target = self.parse_json(index)
        annotations = target.pop("annotations")

        annotation_list = []
        for annotation in annotations:
            bbox = annotation["bounding_box"]

            x = bbox["x"]
            y = bbox["y"]
            w = bbox["w"]
            h = bbox["h"]

            area = w * h
            label = self.classes.index(annotation["name"])

            ann = {
                "area": area,
                "bbox": [x,y,w,h],
                "iscrowd": 0,
                "category_id": label
            }
            annotation_list.append(ann)

        targets = {"annotations": annotation_list, "image_id": index}

        return targets


    def _get_target(self, index: int) -> Dict[str, Tensor]:
        """
        Builds and returns the target dictionary for the item at the given index.
        The returned dictionary has the following structure:

        .. code-block:: python

        [{'boxes': tensor([[0.4289, 0.9250, 0.6047, 0.1500]]),
        'class_labels': tensor([0]),
        'image_id': tensor([1]),
        'area': tensor([318001.1250]),
        'iscrowd': tensor([0]),
        'orig_size': tensor([480, 640]),
        'size': tensor([ 800, 1066])}]

        Parameters
        ----------
        index : int
            The actual index of the item in the ``Dataset``.

        Returns
        -------
        Dict[str, Any]
            The target.
        """
        target = self.parse_json(index)
        annotations = target.pop("annotations")

        targets = []
        for annotation in annotations:
            bbox = annotation["bounding_box"]

            x = bbox["x"]
            y = bbox["y"]
            w = bbox["w"]
            h = bbox["h"]

            bbox = torch.tensor([x, y, w, h])
            area = bbox[2] * bbox[3]
            label = torch.tensor(self.classes.index(annotation["name"]))

            ann = {"boxes": bbox, "area": area, "class_labels": label}

            targets.append(ann)
        # following https://pytorch.org/tutorials/intermediate/torchvision_tutorial.html

        stacked_targets = {
            "boxes": torch.stack([v["boxes"] for v in targets]),
            "area": torch.stack([v["area"] for v in targets]),
            "class_labels": torch.stack([v["class_labels"] for v in targets]),
            "image_id": torch.tensor([index]),
        }


        stacked_targets["iscrowd"] = torch.zeros(len(annotations),)

        return stacked_targets



    def __get_target(self, index: int) -> Dict[str, Tensor]:
        """
        Builds and returns the target dictionary for the item at the given index.
        The returned dictionary has the following structure:

        .. code-block:: python

            {
                "boxes": Tensor,
                "area": Tensor,
                "labels": Tensor,
                "image_id": Tensor,
                "iscrowd": Tensor
            }

        Parameters
        ----------
        index : int
            The actual index of the item in the ``Dataset``.

        Returns
        -------
        Dict[str, Any]
            The target.
        """
        target = self.parse_json(index)
        annotations = target.pop("annotations")

        targets = []
        for annotation in annotations:
            bbox = annotation["bounding_box"]

            x = bbox["x"]
            y = bbox["y"]
            w = bbox["w"]
            h = bbox["h"]

            bbox = torch.tensor([x, y, w, h])
            area = bbox[2] * bbox[3]
            label = torch.tensor(self.classes.index(annotation["name"]))

            ann = {"bbox": bbox, "area": area, "label": label}

            targets.append(ann)
        # following https://pytorch.org/tutorials/intermediate/torchvision_tutorial.html
        stacked_targets = {
            "boxes": torch.stack([v["bbox"] for v in targets]),
            "area": torch.stack([v["area"] for v in targets]),
            "class_labels": torch.stack([v["label"] for v in targets]),
            "image_id": torch.tensor([index]),
        }

        stacked_targets["iscrowd"] = torch.zeros(len(annotations),)

        return stacked_targets


