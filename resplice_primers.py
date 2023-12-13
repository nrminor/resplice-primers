#!/usr/bin/env python3

"""
Module that finds all possible combinations of spike-in primers in an amplicon scheme.

Example usage:
```
usage: resplice_primers.py [-h] --input_bed INPUT_BED [--output_prefix OUTPUT_PREFIX] [--config CONFIG]

options:
    -h, --help            show this help message and exit
    --input_bed INPUT_BED, -i INPUT_BED
                        BED file with one-off spike-in primers to be respliced into possible amplicons.
    --output_prefix OUTPUT_PREFIX, -o OUTPUT_PREFIX
                        Output prefix for final respliced amplicon BED file.
    --config CONFIG, -c CONFIG
                        YAML file used to configure module such that it avoids harcoding
```
"""


import argparse
from itertools import product
from pathlib import Path
from typing import List, Tuple

import polars as pl


def parse_command_line_args() -> Tuple[Path, str]:
    """
        Parse command line arguments while passing errors onto main.

    Args:
        `None`

    Returns:
        `Tuple[Path, str]`: a tuple containing the path to the input BED
        file and a string representing the desired output name.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input_bed",
        "-i",
        type=Path,
        required=True,
        help="BED file with one-off spike-in primers to be respliced into possible amplicons.",
    )
    parser.add_argument(
        "--output_prefix",
        "-o",
        type=str,
        required=False,
        default="respliced",
        help="Output prefix for final respliced amplicon BED file.",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        required=False,
        default="config.yaml",
        help="YAML file used to configure module such that it avoids harcoding",
    )
    args = parser.parse_args()

    return args.input_bed, args.output_prefix


def dedup_primers(partitioned_bed: List[pl.DataFrame]) -> List[pl.DataFrame]:
    """
        `dedup_primers()` primers finds repeated instances of the same primer
        name and adds a unique identifier for each. This ensures that joins
        downstream are one-to-many as opposed to many-to-many.

    Args:
        `partitioned_bed: List[pl.DataFrame]`: A List of Polars DataFrames that
        have been partitioned by amplicon.

    Returns:
        `List[pl.DataFrame]`: A partitioned list of Polars dataframes with no
        repeat primer names.
    """

    for i, df in enumerate(partitioned_bed):
        if True in df.select("NAME").is_duplicated().to_list():
            new_dfs = df.with_columns(
                df.select("NAME").is_duplicated().alias("duped")
            ).partition_by("duped")

            for i, dup_frame in enumerate(new_dfs):
                if True in dup_frame.select("duped").to_series().to_list():
                    renamed = (
                        dup_frame.with_row_count(offset=1)
                        .cast({"row_nr": pl.Utf8})
                        .with_columns(
                            pl.concat_str(
                                [pl.col("NAME"), pl.col("row_nr")],
                                separator="-",
                            ).alias("NAME")
                        )
                        .select(
                            "Ref",
                            "Start Position",
                            "Stop Position",
                            "ORIG_NAME",
                            "NAME",
                            "INDEX",
                            "SENSE",
                            "Gene",
                            "Amplicon",
                            "duped",
                        )
                    )
                    new_dfs[i] = renamed
            df = pl.concat(new_dfs)
            partitioned_bed[i] = df

    dedup_partitioned = (
        pl.concat(partitioned_bed).drop("duped").partition_by("Amplicon")
    )

    return dedup_partitioned


def resolve_primer_names(
    to_combine: List[str], combine_to: List[str]
) -> Tuple[List[str], List[str]]:
    """
        `resolve_primer_names()` names each possible pairing of primers in
        amplicons where singletons, forward or reverse, have been added to
        increase template coverage.

    Args:
        `to_combine: List[str]`: A list of forward primers to resolve.
        `combine_to: List[str]`: A list of reverse primers to resolve.

    Returns:
        `Tuple[List[str], List[str]]`: A tuple containing two lists, the first
        being a list of primer names to use with joining, and the second being
        a list of new primer names to use once left-joining is complete.
    """

    primer_pairs = list(product(to_combine, combine_to))
    primers_to_join = [item[0] for item in primer_pairs] + [
        item[1] for item in primer_pairs
    ]

    new_primer_pairs = [
        [
            "_".join(item[0].split("_")[:-1]).rsplit("-", 1)[0]
            + "_splice"
            + f"{i + 1}"
            + "_"
            + item[0].split("_")[-1],
            "_".join(item[1].split("_")[:-1]).rsplit("-", 1)[0]
            + "_splice"
            + f"{i + 1}"
            + "_"
            + item[1].split("_")[-1],
        ]
        for i, item in enumerate(primer_pairs)
    ]

    new_primer_names = [item[0] for item in new_primer_pairs] + [
        item[1] for item in new_primer_pairs
    ]

    return (primers_to_join, new_primer_names)


def resplice_primers(dedup_partitioned: List[pl.DataFrame]) -> List[pl.DataFrame]:
    """
        `resplice_primers()` determines whether spike-ins are forward or reverse
        primers (or both) and uses that information to handle resplicing
        possible combinations.

    Args:
        `dedup_partitioned: List[pl.DataFrame]`: A Polars dataframe with no
        duplicate primer names.

    Returns:
        `List[pl.DataFrame]`: A list of Polars DataFrames where each dataframe
        is represents all possible pairings of primers within a single amplicon.
    """

    mutated_frames: List[pl.DataFrame] = []
    for i, df in enumerate(dedup_partitioned):
        if df.shape[0] % 2 != 0:
            primers = df["NAME"]

            fwd_primers = [primer for primer in primers if "_LEFT" in primer]
            rev_primers = [primer for primer in primers if "_RIGHT" in primer]

            if len(fwd_primers) > len(rev_primers):
                to_combine = rev_primers
                combine_to = fwd_primers
            elif len(fwd_primers) < len(rev_primers):
                to_combine = fwd_primers
                combine_to = rev_primers
            else:
                break

            primers_to_join, new_primer_names = resolve_primer_names(
                to_combine, combine_to
            )

            assert len(primers_to_join) == len(
                new_primer_names
            ), f"Insufficient number of replacement names generated for partition {i}"

            new_df = (
                pl.DataFrame({"NAME": primers_to_join})
                .join(df, how="left", on="NAME", validate="m:1")
                .with_columns(pl.Series(new_primer_names).alias("NAME"))
                .select(
                    "Ref",
                    "Start Position",
                    "Stop Position",
                    "ORIG_NAME",
                    "NAME",
                    "INDEX",
                    "SENSE",
                    "Gene",
                    "Amplicon",
                )
            )
            mutated_frames.append(new_df)
        else:
            mutated_frames.append(df)

    return mutated_frames


def finalize_primer_pairings(mutated_frames: List[pl.DataFrame]) -> pl.DataFrame:
    """
        `finalize_primer_pairings()` removes any spikeins with possible pairings
        that could not be determined.

    Args:
        `mutated_frames: List[pl.DataFrame]`: A list of Polars DataFrames, each
        representing a respliced amplicon.

    Returns:
        `pl.DataFrame`: A concatenated Polars dataframe that will be written
        out to a new BED file.
    """

    final_frames: List[pl.DataFrame] = []
    for df in mutated_frames:
        fwd_keepers = [
            primer
            for primer in df.select("NAME").to_series().to_list()
            if "_LEFT" in primer
        ]
        rev_keepers = [
            primer
            for primer in df.select("NAME").to_series().to_list()
            if "_RIGHT" in primer
        ]
        if len(fwd_keepers) > 0 and len(rev_keepers) > 0:
            final_frames.append(df)

    final_df = pl.concat(final_frames)

    return final_df


def main() -> None:
    """
    `main()` coordinates the flow of data through the module's functions.
    """

    bed_file, output_prefix = parse_command_line_args()

    partitioned_bed = (
        pl.read_csv(
            bed_file,
            separator="\t",
            has_header=False,
            new_columns=[
                "Ref",
                "Start Position",
                "Stop Position",
                "NAME",
                "INDEX",
                "SENSE",
                "Gene",
            ],
        )
        .with_columns(pl.col("NAME").alias("ORIG_NAME"))
        .with_columns(
            pl.col("NAME")
            .str.replace_all("_LEFT", "")
            .str.replace_all("_RIGHT", "")
            .str.replace_all("-2", "")
            .str.replace_all("-3", "")
            .str.replace_all("-4", "")
            .alias("Amplicon")
        )
                        .select(
                            "Ref",
                            "Start Position",
                            "Stop Position",
                            "ORIG_NAME",
                            "NAME",
                            "INDEX",
                            "SENSE",
                            "Gene",
                            "Amplicon",
                        )
        .with_columns(pl.col("NAME").is_duplicated().alias("duped"))
        .partition_by("Amplicon")
    )

    dedup_partitioned = dedup_primers(partitioned_bed)

    mutated_frames = resplice_primers(dedup_partitioned)

    final_df = finalize_primer_pairings(mutated_frames)

    final_df.drop("Amplicon").sort("Start Position", "Stop Position").write_csv(
        f"{output_prefix}.bed",
        separator="\t",
        include_header=False,
    )


if __name__ == "__main__":
    main()
