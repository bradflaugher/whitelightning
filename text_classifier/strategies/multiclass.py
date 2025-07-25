import logging
import json
import os
from typing import Dict, Optional
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset
import tensorflow as tf
from tensorflow.keras.layers import Embedding, Bidirectional, LSTM, Dropout, Dense
from tensorflow.keras.models import Model
from tensorflow.keras.preprocessing.text import Tokenizer
from tensorflow.keras.preprocessing.sequence import pad_sequences
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import accuracy_score, classification_report
import joblib
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import StringTensorType

from .base import TextClassifierStrategy

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


class TextDataset(Dataset):
    def __init__(self, sequences: np.ndarray, labels: np.ndarray):
        self.sequences = sequences
        self.labels = labels

    def __len__(self) -> int:
        return len(self.sequences)

    def __getitem__(self, idx: int) -> Dict:
        return {
            "input_ids": torch.tensor(self.sequences[idx], dtype=torch.long),
            "labels": torch.tensor(self.labels[idx], dtype=torch.long),
        }


class PyTorchLSTMStrategy(TextClassifierStrategy):
    def __init__(
        self,
        vocab_size: int = 10000,
        embedding_dim: int = 64,
        hidden_dim: int = 64,
        max_len: int = 30,
        output_path: Optional[str] = None,
        **kwargs,
    ):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.vocab_size = vocab_size
        self.embedding_dim = embedding_dim
        self.hidden_dim = hidden_dim
        self.max_len = max_len
        self.tokenizer = Tokenizer(num_words=vocab_size, oov_token="<OOV>")
        self.label_encoder = LabelEncoder()
        self.model = None
        self._is_trained = False
        self.output_path = output_path

    def _build_model(self, num_classes: int) -> nn.Module:
        class TextClassifier(nn.Module):
            def __init__(
                self,
                vocab_size: int,
                embedding_dim: int,
                hidden_dim: int,
                num_classes: int,
            ):
                super().__init__()
                self.embedding = nn.Embedding(vocab_size, embedding_dim, padding_idx=0)
                self.lstm = nn.LSTM(
                    embedding_dim, hidden_dim, batch_first=True, bidirectional=True
                )
                self.dropout = nn.Dropout(0.5)
                self.fc1 = nn.Linear(hidden_dim * 2, 64)
                self.relu = nn.ReLU()
                self.fc2 = nn.Linear(64, num_classes)

            def forward(self, input_ids):
                x = self.embedding(input_ids)
                x, _ = self.lstm(x)
                x = x[:, 0, :]  
                x = self.dropout(x)
                x = self.relu(self.fc1(x))
                return self.fc2(x)

        return TextClassifier(
            self.vocab_size, self.embedding_dim, self.hidden_dim, num_classes
        ).to(self.device)

    def train(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_test: np.ndarray,
        y_test: np.ndarray,
    ) -> Dict:
       
        self.tokenizer.fit_on_texts(np.concatenate([X_train, X_test]))
        X_train_seq = pad_sequences(
            self.tokenizer.texts_to_sequences(X_train),
            maxlen=self.max_len,
            padding="post",
        )
        X_test_seq = pad_sequences(
            self.tokenizer.texts_to_sequences(X_test),
            maxlen=self.max_len,
            padding="post",
        )

    
        y_train_enc = self.label_encoder.fit_transform(y_train)
        y_test_enc = self.label_encoder.transform(y_test)

        
        self.model = self._build_model(num_classes=len(self.label_encoder.classes_))

        
        train_dataset = TextDataset(X_train_seq, y_train_enc)
        train_loader = DataLoader(train_dataset, batch_size=16, shuffle=True)
        criterion = nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam(self.model.parameters(), lr=2e-4)

        
        for epoch in range(10):
            self.model.train()
            total_loss = 0
            for batch in train_loader:
                input_ids = batch["input_ids"].to(self.device)
                labels = batch["labels"].to(self.device)
                optimizer.zero_grad()
                outputs = self.model(input_ids)
                loss = criterion(outputs, labels)
                loss.backward()
                optimizer.step()
                total_loss += loss.item()
            logging.info(
                f"Epoch {epoch+1}/10, Loss: {total_loss/len(train_loader):.4f}"
            )

        self._is_trained = True

       
        train_pred = self.predict(X_train)
        test_pred = self.predict(X_test)

        return {
            "train_accuracy": accuracy_score(y_train_enc, train_pred),
            "test_accuracy": accuracy_score(y_test_enc, test_pred),
            "classification_report": classification_report(
                y_test_enc, test_pred, target_names=self.label_encoder.classes_
            ),
        }

    def predict(self, X) -> np.ndarray:
        self.model.eval()

        X_seq = self.tokenizer.texts_to_sequences(X)
        X_pad = pad_sequences(X_seq, maxlen=self.max_len, padding='post')
        X_tensor = torch.tensor(X_pad, dtype=torch.long).to(self.device)

        with torch.no_grad():
            outputs = self.model(X_tensor)
            return torch.argmax(outputs, dim=1).cpu().numpy()

    def save_model(self):
        if not self._is_trained:
            raise RuntimeError("Cannot save untrained model")
        os.makedirs("models", exist_ok=True)
        torch.save(self.model.state_dict(), f"{self.output_path}/model.pt")
        with open(f"{self.output_path}/vocab.json", "w") as f:
            json.dump(self.tokenizer.word_index, f)
        with open(f"{self.output_path}/scaler.json", "w") as f:
            json.dump(
                {i: label for i, label in enumerate(self.label_encoder.classes_)}, f
            )
        self.export_to_onnx()

    def load_model(self, filename_prefix: str):
        self.model = self._build_model(num_classes=len(self.label_encoder.classes_))
        self.model.load_state_dict(torch.load(f"models/{filename_prefix}_model.pt"))
        self.model.to(self.device)
        with open(f"models/{filename_prefix}_vocab.json", "r") as f:
            self.tokenizer.word_index = json.load(f)
        with open(f"models/{filename_prefix}_scaler.json", "r") as f:
            classes = json.load(f)
            self.label_encoder.classes_ = np.array(
                [classes[str(i)] for i in range(len(classes))]
            )
        self._is_trained = True

    def export_to_onnx(self):
        output_path = f"{self.output_path}/model.onnx"
        if not self._is_trained:
            raise RuntimeError("Model must be trained before exporting to ONNX")
        self.model.eval()
        dummy_input = torch.zeros(1, self.max_len, dtype=torch.long).to(self.device)
        torch.onnx.export(
            self.model,
            dummy_input,
            output_path,
            input_names=["input_ids"],
            output_names=["output"],
            dynamic_axes={"input_ids": {0: "batch_size"}, "output": {0: "batch_size"}},
            opset_version=17,
        )
        logging.info(f"ONNX model saved to {output_path}")


class TensorFlowLSTMStrategy(TextClassifierStrategy):
    def __init__(
        self,
        vocab_size: int = 10000,
        max_len: int = 30,
        output_path: Optional[str] = None,
        **kwargs,
    ):
        self.vocab_size = vocab_size
        self.max_len = max_len
        self.tokenizer = Tokenizer(num_words=vocab_size, oov_token="<OOV>")
        self.label_encoder = LabelEncoder()
        self.model = None  
        self._is_trained = False
        self.output_path = output_path

    def _build_model(self, num_classes: int) -> Model:
        inputs = tf.keras.Input(shape=(self.max_len,), dtype=tf.int32, name="input")
        x = Embedding(self.vocab_size, 64, input_length=self.max_len)(inputs)
        x = Bidirectional(LSTM(64))(x)
        x = Dropout(0.5)(x)
        x = Dense(64, activation="relu")(x)
        outputs = Dense(num_classes, activation="softmax", name="output")(x)
        model = Model(inputs=inputs, outputs=outputs)
        model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="adam",
            metrics=["accuracy"],
        )
        return model

    def train(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_test: np.ndarray,
        y_test: np.ndarray,
    ) -> Dict:
       
        self.tokenizer.fit_on_texts(np.concatenate([X_train, X_test]))
        X_train_seq = pad_sequences(
            self.tokenizer.texts_to_sequences(X_train),
            maxlen=self.max_len,
            padding="post",
        )
        X_test_seq = pad_sequences(
            self.tokenizer.texts_to_sequences(X_test),
            maxlen=self.max_len,
            padding="post",
        )

   
        logging.info(y_test)
        logging.info(y_train)
        y_train_enc = self.label_encoder.fit_transform(y_train)
        y_test_enc = self.label_encoder.transform(y_test)

      
        self.model = self._build_model(num_classes=len(self.label_encoder.classes_))
        history = self.model.fit(
            X_train_seq,
            y_train_enc,
            validation_data=(X_test_seq, y_test_enc),
            epochs=10,
            batch_size=32,
            verbose=1,
        )
        self._is_trained = True

        
        train_pred = np.argmax(self.model.predict(X_train_seq), axis=1)
        test_pred = np.argmax(self.model.predict(X_test_seq), axis=1)

        return {
            "train_accuracy": accuracy_score(y_train_enc, train_pred),
            "test_accuracy": accuracy_score(y_test_enc, test_pred),
            "classification_report": classification_report(
                y_test_enc, test_pred, target_names=self.label_encoder.classes_
            ),
        }

    def predict(self, X_seq) -> np.ndarray:
        return np.argmax(self.model.predict(X_seq), axis=1)

    def save_model(self):
        if not self._is_trained:
            raise RuntimeError("Cannot save untrained model")
        os.makedirs("models", exist_ok=True)
        self.model.save(f"{self.output_path}/model.h5")
        with open(f"{self.output_path}/vocab.json", "w") as f:
            json.dump(self.tokenizer.word_index, f)
        with open(f"{self.output_path}/scaler.json", "w") as f:
            json.dump(
                {i: label for i, label in enumerate(self.label_encoder.classes_)}, f
            )
        self.export_to_onnx()

    def load_model(self, filename_prefix: str):
        self.model = tf.keras.models.load_model(f"models/{filename_prefix}_model.h5")
        with open(f"models/{filename_prefix}_vocab.json", "r") as f:
            self.tokenizer.word_index = json.load(f)
        with open(f"models/{filename_prefix}_scaler.json", "r") as f:
            classes = json.load(f)
            self.label_encoder.classes_ = np.array(
                [classes[str(i)] for i in range(len(classes))]
            )
        self._is_trained = True

    def export_to_onnx(self):
        output_path = f"{self.output_path}/model.onnx"
        if not self._is_trained:
            raise RuntimeError("Model must be trained before exporting to ONNX")
        import tf2onnx

        spec = (tf.TensorSpec((None, self.max_len), tf.int32, name="input"),)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        onnx_model, _ = tf2onnx.convert.from_keras(
            self.model, input_signature=spec, opset=17
        )
        with open(output_path, "wb") as f:
            f.write(onnx_model.SerializeToString())
        logging.info(f"ONNX model saved to {output_path}")


class ScikitLearnTFIDFStrategy(TextClassifierStrategy):
    def __init__(
        self, max_features: int = 10000, output_path: Optional[str] = None, **kwargs
    ):
        self.max_features = max_features
        self.max_len = max_features
        self.pipeline = Pipeline(
            [
                ("tfidf", TfidfVectorizer(max_features=max_features)),
                ("clf", LogisticRegression(max_iter=1000)),
            ]
        )
        self.label_encoder = LabelEncoder()
        self._is_trained = False
        self.output_path = output_path

    def train(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_test: np.ndarray,
        y_test: np.ndarray,
    ) -> Dict:
        
        y_train_enc = self.label_encoder.fit_transform(y_train)
        y_test_enc = self.label_encoder.transform(y_test)

      
        self.pipeline.fit(X_train, y_train_enc)
        self._is_trained = True

       
        train_pred = self.pipeline.predict(X_train)
        test_pred = self.pipeline.predict(X_test)

        return {
            "train_accuracy": accuracy_score(y_train_enc, train_pred),
            "test_accuracy": accuracy_score(y_test_enc, test_pred),
            "classification_report": classification_report(
                y_test_enc, test_pred, target_names=self.label_encoder.classes_
            ),
        }

    def predict(self, X: np.ndarray) -> np.ndarray:
        if not self._is_trained:
            raise RuntimeError("Model must be trained before predicting")
        return self.pipeline.predict(X)

    def save_model(self):
        if not self._is_trained:
            raise RuntimeError("Cannot save untrained model")
        joblib.dump(self.pipeline, f"{self.output_path}/model.pkl")
        vocab = {
            k: int(v) for k, v in self.pipeline.named_steps["tfidf"].vocabulary_.items()
        }
        with open(f"{self.output_path}/vocab.json", "w") as f:
            json.dump(vocab, f)
        with open(f"{self.output_path}/scaler.json", "w") as f:
            json.dump(
                {i: label for i, label in enumerate(self.label_encoder.classes_)}, f
            )
        self.export_to_onnx()

    def load_model(self, filename_prefix: str):
        self.pipeline = joblib.load(f"models/{filename_prefix}_model.pkl")
        with open(f"models/{filename_prefix}_vocab.json", "r") as f:
            vocab = json.load(f)
            self.pipeline.named_steps["tfidf"].vocabulary_ = vocab
        with open(f"models/{filename_prefix}_scaler.json", "r") as f:
            classes = json.load(f)
            self.label_encoder.classes_ = np.array(
                [classes[str(i)] for i in range(len(classes))]
            )
        self._is_trained = True

    def export_to_onnx(self):
        output_path = f"{self.output_path}/model.onnx"
        if not self._is_trained:
            raise RuntimeError("Model must be trained before exporting to ONNX")
        initial_type = [("input", StringTensorType([None]))]
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        onnx_model = convert_sklearn(
            self.pipeline, initial_types=initial_type, target_opset=17
        )
        with open(output_path, "wb") as f:
            f.write(onnx_model.SerializeToString())
        logging.info(f"ONNX model saved to {output_path}")
