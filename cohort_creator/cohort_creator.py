"""Install a set of datalad datasets from openneuro and get the data for a set of participants.

Then copy the data to a new directory structure to create a "cohort".

"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pandas as pd
from datalad import api
from datalad.support.exceptions import (
    IncompleteResultsError,
)

from cohort_creator._utils import _is_dataset_in_openneuro
from cohort_creator._utils import copy_top_files
from cohort_creator._utils import dataset_path
from cohort_creator._utils import filter_excluded_participants
from cohort_creator._utils import get_participant_ids
from cohort_creator._utils import get_sessions
from cohort_creator._utils import is_subject_in_dataset
from cohort_creator._utils import list_all_files
from cohort_creator._utils import no_files_found_msg
from cohort_creator._utils import openneuro_df
from cohort_creator.logger import cc_logger


cc_log = cc_logger()

logging.getLogger("datalad").setLevel(logging.WARNING)


def install_datasets(datasets: list[str], sourcedata: Path, dataset_types: list[str]) -> None:
    """Will install several datalad datasets from openneuro.

    Parameters
    ----------
    datasets : list[str]
        List of dataset names.

        Example: ``["ds000001", "ds000002"]``

    sourcedata : Path
        Path where the datasets will be installed.

    dataset_types : list[str]
        Can contain any of: ``"raw"``, ``"fmriprep"``, ``"mriqc"``.

    """
    cc_log.info("Installing datasets")
    for dataset_ in datasets:
        cc_log.info(f" {dataset_}")
        _install(dataset_name=dataset_, dataset_types=dataset_types, output_path=sourcedata)


def _install(dataset_name: str, dataset_types: list[str], output_path: Path) -> None:
    if not _is_dataset_in_openneuro(dataset_name):
        cc_log.warning(f"  {dataset_name} not found in openneuro")
        return None

    openneuro = openneuro_df()
    mask = openneuro.name == dataset_name
    dataset_df = openneuro[mask]

    for dataset_type_ in dataset_types:
        derivative = None if dataset_type_ == "raw" else dataset_type_

        data_pth = dataset_path(output_path, dataset_name, derivative=derivative)

        if data_pth.exists():
            cc_log.info(f"  {dataset_type_} data already present at {data_pth}")
        else:
            cc_log.info(f"    installing {dataset_type_} data at: {data_pth}")
            if uri := dataset_df[dataset_type_].values[0]:
                api.install(path=data_pth, source=uri)


def get_data(
    datasets: pd.DataFrame,
    sourcedata: Path,
    participants: pd.DataFrame,
    dataset_types: list[str],
    datatypes: list[str],
    space: str,
    jobs: int,
) -> None:
    """Get the data for specified participants / datatypes / space \
    from preinstalled datalad datasets / dataset_types.

    Parameters
    ----------
    datasets : pd.DataFrame

    sourcedata : Path

    participants : pd.DataFrame

    dataset_types : list[str]
        Can contain any of: ``"raw"``, ``"fmriprep"``, ``"mriqc"``.

    datatypes : list[str]
        Can contain any of: ``"anat'``, ``"func"``

    space : str
        Space of the data to get (only applies when dataset_types requested includes fmriprep).

    jobs : int
        Number of jobs to use for parallelization during datalad get operation.

    """
    cc_log.info("Getting data")

    if isinstance(datatypes, str):
        datatypes = [datatypes]

    for dataset_ in datasets["DatasetName"]:
        cc_log.info(f" {dataset_}")

        participants_ids = get_participant_ids(participants, dataset_)
        if not participants_ids:
            cc_log.warning(f"  no participants in dataset {dataset_}")
            continue

        cc_log.info(f"  getting data for: {participants_ids}")

        for dataset_type_ in dataset_types:
            cc_log.info(f"  {dataset_type_}")

            derivative = None if dataset_type_ == "raw" else dataset_type_

            data_pth = dataset_path(sourcedata, dataset_, derivative=derivative)

            dl_dataset = api.Dataset(data_pth)

            for subject in participants_ids:
                if not is_subject_in_dataset(subject, data_pth):
                    cc_log.warning(f"  no participant {subject} in dataset {dataset_}")
                    continue
                sessions = get_sessions(participants, dataset_, subject)
                _get_data_this_subject(
                    subject=subject,
                    sessions=sessions,
                    datatypes=datatypes,
                    space=space,
                    dataset_type=dataset_type_,
                    data_pth=data_pth,
                    dl_dataset=dl_dataset,
                    jobs=jobs,
                )


def _get_data_this_subject(
    subject: str,
    sessions: list[str] | list[None],
    datatypes: list[str],
    space: str,
    dataset_type: str,
    data_pth: Path,
    dl_dataset: api.Dataset,
    jobs: int,
) -> None:
    for datatype_ in datatypes:
        files = list_all_files(
            data_pth=data_pth,
            dataset_type=dataset_type,
            subject=subject,
            sessions=sessions,
            datatype=datatype_,
            space=space,
        )
        if not files:
            cc_log.warning(no_files_found_msg(subject, datatype_))
            continue
        cc_log.info(f"    {subject} - getting files:\n     {files}")
        try:
            dl_dataset.get(path=files, jobs=jobs)
        except IncompleteResultsError:
            cc_log.error(f"    {subject} - failed to get files:\n     {files}")


def construct_cohort(
    datasets: pd.DataFrame,
    output_dir: Path,
    sourcedata_dir: Path,
    participants: pd.DataFrame,
    dataset_types: list[str],
    datatypes: list[str],
    space: str,
) -> None:
    """Copy the data from sourcedata_dir to output_dir, to create a cohort.

    Parameters
    ----------
    datasets : pd.DataFrame

    output_dir : Path

    sourcedata_dir : Path

    participants : pd.DataFrame

    dataset_types : list[str]
        Can contain any of: ``"raw"``, ``"fmriprep"``, ``"mriqc"``.

    datatypes : list[str]
        Can contain any of: ``"anat'``, ``"func"``

    space : str
        Space of the data to get (only applies when dataset_types requested includes fmriprep).

    """
    cc_log.info("Constructing cohort")

    for dataset_ in datasets["DatasetName"]:
        cc_log.info(f" {dataset_}")

        participants_ids = get_participant_ids(participants, dataset_)
        if not participants_ids:
            cc_log.warning(f"  no participants in dataset {dataset_}")
            continue

        cc_log.info(f"  creating cohort with: {participants_ids}")

        for dataset_type_ in dataset_types:
            cc_log.info(f"  {dataset_type_}")

            derivative = None if dataset_type_ == "raw" else dataset_type_

            src_dir = dataset_path(sourcedata_dir, dataset_, derivative=derivative)

            study_dir = f"study-{dataset_}"
            if dataset_type_ == "raw":
                target_dir = dataset_path(output_dir, study_dir)
            else:
                target_dir = output_dir / study_dir / "derivatives" / dataset_type_
            target_dir.mkdir(exist_ok=True, parents=True)

            copy_top_files(src_dir=src_dir, target_dir=target_dir, datatypes=datatypes)
            filter_excluded_participants(pth=target_dir, participants=participants_ids)

            for subject in participants_ids:
                if not is_subject_in_dataset(subject, src_dir):
                    cc_log.warning(f"  no participant {subject} in dataset {dataset_}")
                    continue
                sessions = get_sessions(participants, dataset_, subject)
                _copy_this_subject(
                    subject=subject,
                    sessions=sessions,
                    datatypes=datatypes,
                    dataset_type=dataset_type_,
                    space=space,
                    src_dir=src_dir,
                    target_dir=target_dir,
                )


def _copy_this_subject(
    subject: str,
    sessions: list[str] | list[None],
    datatypes: list[str],
    dataset_type: str,
    space: str,
    src_dir: Path,
    target_dir: Path,
) -> None:
    for datatype_ in datatypes:
        files = list_all_files(
            data_pth=src_dir,
            dataset_type=dataset_type,
            subject=subject,
            sessions=sessions,
            datatype=datatype_,
            space=space,
        )
        if not files:
            cc_log.warning(no_files_found_msg(subject, datatype_))
            continue

        cc_log.info(f"    {subject} - copying files:\n     {files}")
        for f in files:
            sub_dirs = Path(f).parents
            (target_dir / sub_dirs[0]).mkdir(exist_ok=True, parents=True)
            if (target_dir / f).exists():
                cc_log.info(f"      file '{f}' already present")
                continue
            try:
                shutil.copy(src=src_dir / f, dst=target_dir / f, follow_symlinks=True)
                # TODO deal with permission
            except FileNotFoundError:
                cc_log.error(f"      Could not find file '{f}'")
