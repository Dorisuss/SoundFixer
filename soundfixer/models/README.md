# Нейросеть шумоподавления DeepFilterNet3

- `DeepFilterNet3_ll_onnx.tar.gz` — модель с низкой задержкой (10 мс), используется по умолчанию.
- `DeepFilterNet3_onnx.tar.gz` — лёгкая модель (задержка 30 мс, вдвое меньше нагрузки).
- `../bin/deepfilter.dll` — потоковая реализация libDF (Rust + tract) с C-интерфейсом
  (`df_create`, `df_process_frame`, `df_set_atten_lim`, `df_free`).

Источник: https://github.com/Rikorose/DeepFilterNet (коммит d375b2d8),
H. Schröter и др., «DeepFilterNet: Perceptually Motivated Real-Time Speech Enhancement».
Лицензия: MIT или Apache-2.0 на выбор — тексты лежат рядом. DLL собирается скриптом
`scripts/build_deepfilter.sh`; программа проверяет модели по SHA-256 перед загрузкой.
