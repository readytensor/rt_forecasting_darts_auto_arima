import os
import warnings
import joblib
import numpy as np
import pandas as pd
from tqdm import tqdm
from typing import Optional
from darts.models.forecasting.auto_arima import AutoARIMA
from darts import TimeSeries
from schema.data_schema import ForecastingSchema
from sklearn.exceptions import NotFittedError
from multiprocessing import cpu_count, Pool

warnings.filterwarnings("ignore")


PREDICTOR_FILE_NAME = "predictor.joblib"

# Determine the number of CPUs available
n_cpus = cpu_count()

# Determine the number of CPUs available
CPUS_TO_USE = max(1, cpu_count() - 1)  # spare one CPU for other tasks
NUM_CPUS_PER_BATCH = 1  # Number of CPUs each batch can use


class Forecaster:
    """A wrapper class for the AutoARIMA Forecaster.

    This class provides a consistent interface that can be used with other
    Forecaster models.
    """

    model_name = "AutoARIMA Forecaster"

    def __init__(
        self,
        data_schema: ForecastingSchema,
        history_forecast_ratio: int = None,
        add_encoders: Optional[dict] = None,
        **autoarima_kwargs,
    ):
        """Construct a new AutoARIMA Forecaster

        Args:

            data_schema (ForecastingSchema):
                The schema of the training data.

            history_forecast_ratio (int):
                Sets the history length depending on the forecast horizon.
                For example, if the forecast horizon is 20 and the history_forecast_ratio is 10,
                history length will be 20*10 = 200 samples.

            add_encoders (Optional[dict]): A large number of future covariates can be automatically generated with add_encoders. This can be done by adding multiple pre-defined index encoders and/or custom user-made functions that will be used as index encoders. Additionally, a transformer such as Darts' Scaler can be added to transform the generated covariates. This happens all under one hood and only needs to be specified at model creation. Read SequentialEncoder to find out more about add_encoders. Default: None. An example showing some of add_encoders features:

            def encode_year(idx):
                return (idx.year - 1950) / 50

            add_encoders={
                'cyclic': {'future': ['month']},
                'datetime_attribute': {'future': ['hour', 'dayofweek']},
                'position': {'future': ['relative']},
                'custom': {'future': [encode_year]},
                'transformer': Scaler(),
                'tz': 'CET'
            }
            autoarima_kwargs: Keyword arguments for the pmdarima.AutoARIMA model
        """
        self.data_schema = data_schema
        self.history_forecast_ratio = history_forecast_ratio
        self._is_trained = False
        self.add_encoders = add_encoders
        self.models = {}
        self.autoarima_kwargs = autoarima_kwargs
        self.history_length = None

        if history_forecast_ratio:
            self.history_length = data_schema.forecast_length * history_forecast_ratio

    def fit(
        self,
        history: pd.DataFrame,
        data_schema: ForecastingSchema,
    ) -> None:
        """Fit the Forecaster to the training data.
        A separate AutoARIMA model is fit to each series that is contained
        in the data.

        Args:
            history (pandas.DataFrame): The features of the training data.
            data_schema (ForecastingSchema): The schema of the training data.

        """
        np.random.seed(0)
        groups_by_ids = history.groupby(data_schema.id_col)
        all_ids = list(groups_by_ids.groups.keys())
        all_series = [
            groups_by_ids.get_group(id_).drop(columns=data_schema.id_col)
            for id_ in all_ids
        ]

        # Prepare batches of series to be processed in parallel
        num_parallel_batches = CPUS_TO_USE // NUM_CPUS_PER_BATCH
        if len(all_ids) <= num_parallel_batches:
            series_per_batch = 1
        else:
            series_per_batch = 1 + (len(all_ids) // num_parallel_batches)
        series_batches = [
            all_series[i : i + series_per_batch]
            for i in range(0, len(all_series), series_per_batch)
        ]
        id_batches = [
            all_ids[i : i + series_per_batch]
            for i in range(0, len(all_ids), series_per_batch)
        ]

        # Use multiprocessing to fit models in parallel
        with Pool(processes=len(series_batches)) as pool:
            results = pool.starmap(
                self.fit_batch_of_series,
                zip(series_batches, id_batches, [data_schema] * len(series_batches)),
            )

        # Flatten results and update the models dictionary
        self.models = {id: model for batch in results for id, model in batch.items()}

        self.all_ids = all_ids
        self._is_trained = True
        self.data_schema = data_schema

    def fit_batch_of_series(self, series_batch, ids_batch, data_schema):
        models = {}
        for series, id in zip(series_batch, ids_batch):
            if self.history_length:
                series = series[-self.history_length :]
            model = self._fit_on_series(history=series, data_schema=data_schema)
            models[id] = model
        return models

    def _fit_on_series(self, history: pd.DataFrame, data_schema: ForecastingSchema):
        """Fit AutoARIMA model to given individual series of data"""
        model = AutoARIMA(add_encoders=self.add_encoders, **self.autoarima_kwargs)
        series = TimeSeries.from_dataframe(
            history, data_schema.time_col, data_schema.target
        )
        future_covariates = None
        if data_schema.future_covariates + data_schema.static_covariates:
            future_covariates = TimeSeries.from_dataframe(
                history,
                data_schema.time_col,
                data_schema.future_covariates + data_schema.static_covariates,
            )

        model.fit(series, future_covariates=future_covariates)

        return model

    def predict(
        self, test_data: pd.DataFrame, prediction_col_name: str
    ) -> pd.DataFrame:
        """Make the forecast of given length.

        Args:
            test_data (pd.DataFrame): Given test input for forecasting.
            prediction_col_name (str): Name to give to prediction column.
        Returns:
            pd.DataFrame: The prediction dataframe.
        """
        if not self._is_trained:
            raise NotFittedError("Model is not fitted yet.")

        groups_by_ids = test_data.groupby(self.data_schema.id_col)
        all_series = [
            groups_by_ids.get_group(id_).drop(columns=self.data_schema.id_col)
            for id_ in self.all_ids
        ]
        # forecast one series at a time
        all_forecasts = []
        for id_, series_df in zip(self.all_ids, all_series):
            forecast = self._predict_on_series(key_and_future_df=(id_, series_df))
            forecast.insert(0, self.data_schema.id_col, id_)
            all_forecasts.append(forecast)

        # concatenate all series' forecasts into a single dataframe
        all_forecasts = pd.concat(all_forecasts, axis=0, ignore_index=True)

        all_forecasts.rename(
            columns={self.data_schema.target: prediction_col_name}, inplace=True
        )
        return all_forecasts

    def _predict_on_series(self, key_and_future_df):
        """Make forecast on given individual series of data"""
        key, future_df = key_and_future_df
        covariates = None
        covariates_names = (
            self.data_schema.future_covariates + self.data_schema.static_covariates
        )
        if covariates_names:
            covariates = TimeSeries.from_dataframe(
                future_df,
                self.data_schema.time_col,
                covariates_names,
            )

        if self.models.get(key) is not None:
            forecast = self.models[key].predict(
                len(future_df), future_covariates=covariates
            )
            forecast_df = forecast.pd_dataframe()
            forecast = forecast_df[self.data_schema.target]
            future_df[self.data_schema.target] = forecast.values

        else:
            # no model found - key wasnt found in history, so cant forecast for it.
            future_df = None

        return future_df

    def save(self, model_dir_path: str) -> None:
        """Save the Forecaster to disk.

        Args:
            model_dir_path (str): Dir path to which to save the model.
        """
        if not self._is_trained:
            raise NotFittedError("Model is not fitted yet.")
        joblib.dump(self, os.path.join(model_dir_path, PREDICTOR_FILE_NAME))

    @classmethod
    def load(cls, model_dir_path: str) -> "Forecaster":
        """Load the Forecaster from disk.

        Args:
            model_dir_path (str): Dir path to the saved model.
        Returns:
            Forecaster: A new instance of the loaded Forecaster.
        """
        model = joblib.load(os.path.join(model_dir_path, PREDICTOR_FILE_NAME))
        return model

    def __str__(self):
        # sort params alphabetically for unit test to run successfully
        return f"Model name: {self.model_name}"


def train_predictor_model(
    history: pd.DataFrame,
    data_schema: ForecastingSchema,
    hyperparameters: dict,
) -> Forecaster:
    """
    Instantiate and train the predictor model.

    Args:
        history (pd.DataFrame): The training data inputs.
        data_schema (ForecastingSchema): Schema of the training data.
        hyperparameters (dict): Hyperparameters for the Forecaster.

    Returns:
        'Forecaster': The Forecaster model
    """

    model = Forecaster(
        data_schema=data_schema,
        **hyperparameters,
    )
    model.fit(history=history, data_schema=data_schema)
    return model


def predict_with_model(
    model: Forecaster, test_data: pd.DataFrame, prediction_col_name: str
) -> pd.DataFrame:
    """
    Make forecast.

    Args:
        model (Forecaster): The Forecaster model.
        test_data (pd.DataFrame): The test input data for forecasting.
        prediction_col_name (int): Name to give to prediction column.

    Returns:
        pd.DataFrame: The forecast.
    """
    return model.predict(test_data, prediction_col_name)


def save_predictor_model(model: Forecaster, predictor_dir_path: str) -> None:
    """
    Save the Forecaster model to disk.

    Args:
        model (Forecaster): The Forecaster model to save.
        predictor_dir_path (str): Dir path to which to save the model.
    """
    if not os.path.exists(predictor_dir_path):
        os.makedirs(predictor_dir_path)
    model.save(predictor_dir_path)


def load_predictor_model(predictor_dir_path: str) -> Forecaster:
    """
    Load the Forecaster model from disk.

    Args:
        predictor_dir_path (str): Dir path where model is saved.

    Returns:
        Forecaster: A new instance of the loaded Forecaster model.
    """
    return Forecaster.load(predictor_dir_path)
