import os
import math
import shutil
import tempfile
import zipfile
from contextlib import contextmanager

from qgis.PyQt.QtCore import QCoreApplication, QTimer, Qt
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QDoubleSpinBox,
    QSpinBox,
    QVBoxLayout,
    QToolButton,
    QWidget,
)
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsMessageLog,
    QgsProject,
    QgsRasterLayer,
    Qgis,
)
from qgis.gui import QgsFileWidget, QgsProjectionSelectionWidget


def xml_escape(value):
    """Escape text used inside XML elements."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _qt_enum(owner, name, legacy_name=None):
    legacy_name = legacy_name or name
    scoped = getattr(owner, legacy_name, None)
    if scoped is not None:
        return scoped
    for nested_name in (
        "ItemFlag", "ItemFlags", "WindowModality", "CheckState", "ButtonRole",
        "StorageMode"
    ):
        nested = getattr(owner, nested_name, None)
        if nested is not None:
            value = getattr(nested, name, None)
            if value is not None:
                return value
    raise AttributeError("Qt compatibility enum not found: {}.{}".format(owner, name))


QT_ITEM_USER_CHECKABLE = _qt_enum(Qt, "ItemIsUserCheckable")
QT_ITEM_ENABLED = _qt_enum(Qt, "ItemIsEnabled")
QT_UNCHECKED = _qt_enum(Qt, "Unchecked", "Unchecked")
QT_CHECKED = _qt_enum(Qt, "Checked", "Checked")
QT_WINDOW_MODAL = _qt_enum(Qt, "WindowModal")
QT_ACCEPT_ROLE = _qt_enum(QDialogButtonBox, "AcceptRole")
QT_REJECT_ROLE = _qt_enum(QDialogButtonBox, "RejectRole")
QT_STORAGE_GET_DIRECTORY = _qt_enum(QgsFileWidget, "GetDirectory", "GetDirectory")
QT_STORAGE_SAVE_FILE = _qt_enum(QgsFileWidget, "SaveFile", "SaveFile")


def log_error(message, error=None):
    text = str(message)
    if error is not None:
        text += "\n{}".format(error)
    message_level = getattr(Qgis, "Warning", None)
    if message_level is None:
        message_level = Qgis.MessageLevel.Warning
    QgsMessageLog.logMessage(text, "kk_txt2dem", message_level)


class KkTxt2DemDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("グリッドまたはグランドCSVを標高DEMに変換")
        self.resize(620, 520)
        self._entries = {}

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("　CSVファイルまたはZIPファイルのフォルダを選択"))
        folder_layout = QHBoxLayout()
        self.folder_widget = QgsFileWidget()
        self.folder_widget.setStorageMode(QT_STORAGE_GET_DIRECTORY)
        self.folder_widget.fileChanged.connect(self._load_zip_files)
        for button in (
            self.folder_widget.findChildren(QPushButton)
            + self.folder_widget.findChildren(QToolButton)
        ):
            button.clicked.connect(
                lambda checked=False: QTimer.singleShot(
                    150, self._reload_selected_folder
                )
            )
        folder_layout.addWidget(self.folder_widget)
        layout.addLayout(folder_layout)

        layout.addWidget(QLabel("　ZIP内の標高値テキスト (*.txt)"))
        self.file_list = QListWidget()
        layout.addWidget(self.file_list)

        select_layout = QHBoxLayout()
        select_all_button = QPushButton("すべて選択")
        select_all_button.clicked.connect(self._select_all)
        clear_all_button = QPushButton("選択解除")
        clear_all_button.clicked.connect(self._clear_all)
        select_layout.addWidget(select_all_button)
        select_layout.addWidget(clear_all_button)
        select_layout.addStretch()
        layout.addLayout(select_layout)

        form_layout = QFormLayout()
        columns_layout = QHBoxLayout()
        self.x_column = self._column_spinbox(2)
        self.y_column = self._column_spinbox(3)
        self.z_column = self._column_spinbox(4)
        columns_layout.addWidget(QLabel("X列"))
        columns_layout.addWidget(self.x_column)
        columns_layout.addWidget(QLabel("Y列"))
        columns_layout.addWidget(self.y_column)
        columns_layout.addWidget(QLabel("標高列"))
        columns_layout.addWidget(self.z_column)
        columns_layout.addStretch()
        form_layout.addRow("", columns_layout)

        self.grid_size = QDoubleSpinBox()
        self.grid_size.setRange(0.0, 1000000.0)
        self.grid_size.setDecimals(3)
        self.grid_size.setValue(0.5)
        self.grid_size.setSingleStep(0.5)
        self.grid_size.setSuffix(" 座標単位（0で自動）")
        form_layout.addRow("グリッドサイズ", self.grid_size)
        self.interpolate_nodata = QCheckBox("Nodata値の補間を行う（処理に時間がかかります）")
        form_layout.addRow("", self.interpolate_nodata)

        self.crs_widget = QgsProjectionSelectionWidget()
        self.crs_widget.setCrs(QgsCoordinateReferenceSystem("EPSG:6677"))
        form_layout.addRow("座標参照系", self.crs_widget)

        self.output_widget = QgsFileWidget()
        self.output_widget.setStorageMode(QT_STORAGE_SAVE_FILE)
        self.output_widget.setFilter("GeoTIFF (*.tif *.tiff)")
        form_layout.addRow("出力ファイル", self.output_widget)
        layout.addLayout(form_layout)

        buttons = QDialogButtonBox()
        self.convert_button = QPushButton("変換開始")
        self.convert_button.setDefault(True)
        cancel_button = QPushButton("閉じる")
        buttons.addButton(self.convert_button, QT_ACCEPT_ROLE)
        buttons.addButton(cancel_button, QT_REJECT_ROLE)
        layout.addWidget(buttons)
        cancel_button.clicked.connect(self.reject)

    def _column_spinbox(self, value):
        spinbox = QSpinBox()
        spinbox.setRange(1, 999)
        spinbox.setValue(value)
        return spinbox

    def _load_zip_files(self, folder):
        self.file_list.clear()
        self._entries = {}
        if not folder or not os.path.isdir(folder):
            return
        try:
            folder_entries = sorted(os.listdir(folder))
        except (OSError, PermissionError) as error:
            QMessageBox.critical(self, "kk_txt2dem", "フォルダーを読み取れません。\n{}".format(error))
            log_error("フォルダーの読み取りに失敗しました: {}".format(folder), error)
            return

        row = 0
        for filename in folder_entries:
            file_path = os.path.join(folder, filename)
            extension = os.path.splitext(filename)[1].lower()
            if extension in (".txt", ".csv"):
                item = QListWidgetItem(filename)
                item.setFlags(item.flags() | QT_ITEM_USER_CHECKABLE)
                item.setCheckState(QT_UNCHECKED)
                self.file_list.addItem(item)
                self._entries[row] = ("file", file_path, None)
                row += 1
                continue
            if extension != ".zip":
                continue
            zip_path = file_path
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    for member in archive.infolist():
                        if member.is_dir():
                            continue
                        member_extension = os.path.splitext(member.filename)[1].lower()
                        if member_extension not in (".txt", ".csv"):
                            continue
                        item = QListWidgetItem(
                            "{} / {}".format(filename, member.filename)
                        )
                        item.setFlags(item.flags() | QT_ITEM_USER_CHECKABLE)
                        item.setCheckState(QT_UNCHECKED)
                        self.file_list.addItem(item)
                        self._entries[row] = ("zip", zip_path, member.filename)
                        row += 1
            except (OSError, PermissionError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
                item = QListWidgetItem("{}（読み込み不可: {}）".format(filename, error))
                item.setFlags(item.flags() & ~QT_ITEM_ENABLED)
                self.file_list.addItem(item)
                row += 1
                log_error("{} を読み取れませんでした。".format(zip_path), error)

    def _select_all(self):
        for row in range(self.file_list.count()):
            item = self.file_list.item(row)
            if item.flags() & QT_ITEM_ENABLED:
                item.setCheckState(QT_CHECKED)

    def _clear_all(self):
        for row in range(self.file_list.count()):
            item = self.file_list.item(row)
            item.setCheckState(QT_UNCHECKED)

    def _reload_selected_folder(self):
        folder = self.folder_widget.filePath()
        if folder:
            self._load_zip_files(folder)

    def selected_entries(self):
        selected = []
        for row in range(self.file_list.count()):
            item = self.file_list.item(row)
            if item.checkState() == QT_CHECKED and row in self._entries:
                selected.append(self._entries[row])
        return selected

    def validate(self):
        selected = self.selected_entries()
        output = self.output_widget.filePath()
        if not selected:
            QMessageBox.warning(self, "kk_txt2dem", "変換するテキストファイルを選択してください。")
            return None
        if not output:
            QMessageBox.warning(self, "kk_txt2dem", "出力ファイルを指定してください。")
            return None
        if not self.crs_widget.crs().isValid():
            QMessageBox.warning(self, "kk_txt2dem", "座標参照系を指定してください。")
            return None
        output_dir = os.path.dirname(output)
        if output_dir and not os.path.isdir(output_dir):
            QMessageBox.warning(self, "kk_txt2dem", "出力フォルダーが存在しません。\n{}".format(output_dir))
            return None
        if output_dir:
            try:
                with open(os.path.join(output_dir, ".kk_txt2dem_write_test"), "w", encoding="utf-8"):
                    pass
                os.remove(os.path.join(output_dir, ".kk_txt2dem_write_test"))
            except OSError as error:
                QMessageBox.critical(self, "kk_txt2dem", "出力フォルダーに書き込めません。\n{}".format(error))
                log_error("出力フォルダーへの書き込み確認に失敗しました。", error)
                return None
        if not output.lower().endswith((".tif", ".tiff")):
            output += ".tif"
            self.output_widget.setFilePath(output)
        return selected


class KkTxt2DemPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.png"))
        self.action = QAction(icon, "kk_txt2dem", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        if hasattr(self.iface, "addPluginToRasterMenu"):
            self.iface.addPluginToRasterMenu("kk_txt2dem", self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        if hasattr(self.iface, "removePluginRasterMenu"):
            self.iface.removePluginRasterMenu("kk_txt2dem", self.action)
        elif hasattr(self.iface, "removePluginFromRasterMenu"):
            self.iface.removePluginFromRasterMenu("kk_txt2dem", self.action)

    def run(self):
        if self.dialog is None:
            self.dialog = KkTxt2DemDialog(self.iface.mainWindow())
            self.dialog.convert_button.clicked.connect(self.convert)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def convert(self):
        selected = self.dialog.validate()
        if selected is None:
            return
        if not getattr(self.dialog, "convert_button", None):
            return

        output = self.dialog.output_widget.filePath()
        x_column = self.dialog.x_column.value() - 1
        y_column = self.dialog.y_column.value() - 1
        z_column = self.dialog.z_column.value() - 1
        crs = self.dialog.crs_widget.crs()
        grid_size = self.dialog.grid_size.value()
        interpolate_nodata = self.dialog.interpolate_nodata.isChecked()

        progress = QProgressDialog(
            "標高DEMを作成しています...", "キャンセル", 0, len(selected),
            self.dialog
        )
        progress.setWindowModality(QT_WINDOW_MODAL)
        progress.setMinimumDuration(0)

        temp_rasters = []
        try:
            temp_directory = tempfile.mkdtemp(prefix="kk_txt2dem_")
            if not os.path.isdir(temp_directory):
                raise OSError("作業フォルダーを作成できませんでした。")
            for index, source_entry in enumerate(selected, start=1):
                if progress.wasCanceled():
                    raise UserWarning("処理はキャンセルされました。")
                progress.setValue(index - 1)
                source_kind, source_path, member_name = source_entry
                display_name = (
                    member_name if source_kind == "zip"
                    else os.path.basename(source_path)
                )
                progress.setLabelText("{} を変換中".format(display_name))

                temp_raster = os.path.join(
                    temp_directory,
                    "kk_txt2dem_{:03d}.tif".format(index)
                )
                self._convert_text(
                    source_path, member_name, temp_raster, x_column, y_column,
                    z_column, crs, grid_size, progress, interpolate_nodata
                )
                temp_rasters.append(temp_raster)
                progress.setValue(index)

            if progress.wasCanceled():
                raise UserWarning("処理はキャンセルされました。")
            progress.setLabelText("選択したDEMを結合中...")
            QCoreApplication.processEvents()
            self._merge_rasters(temp_rasters, output, crs)
            layer = QgsRasterLayer(output, os.path.splitext(os.path.basename(output))[0])
            if not layer.isValid():
                raise RuntimeError("出力ラスターを読み込めませんでした。")
            QgsProject.instance().addMapLayer(layer)
            QMessageBox.information(
                self.dialog, "kk_txt2dem", "変換が完了しました。\n{}".format(output)
            )
        except UserWarning as warning:
            QMessageBox.information(self.dialog, "kk_txt2dem", str(warning))
            log_error(str(warning))
        except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            QMessageBox.critical(self.dialog, "kk_txt2dem", "ZIPファイルを処理できません。\n{}".format(error))
            log_error("ZIP処理に失敗しました。", error)
        except (PermissionError, OSError, IOError) as error:
            QMessageBox.critical(self.dialog, "kk_txt2dem", "ファイルI/Oエラーが発生しました。\n{}".format(error))
            log_error("ファイルI/Oエラーが発生しました。", error)
        except Exception as error:
            if not isinstance(error, UserWarning):
                QMessageBox.critical(self.dialog, "kk_txt2dem", str(error))
                log_error("予期しない変換エラーが発生しました。", error)
            else:
                QMessageBox.information(self.dialog, "kk_txt2dem", str(error))
        finally:
            progress.close()
            for raster in temp_rasters:
                for suffix in ("", ".aux.xml"):
                    try:
                        os.remove(raster + suffix if suffix else raster)
                    except OSError:
                        pass
            if 'temp_directory' in locals():
                shutil.rmtree(temp_directory, ignore_errors=True)

    def _convert_text(
        self, zip_path, member_name, output, x_column, y_column, z_column, crs,
        grid_size=0.0, progress=None, interpolate_nodata=False
    ):
        display_name = member_name or os.path.basename(zip_path)
        try:
            from osgeo import gdal, osr
        except ImportError as error:
            raise RuntimeError("QGIS GDALモジュールを読み込めませんでした。") from error

        gdal.UseExceptions()
        spatial_reference = osr.SpatialReference()
        if spatial_reference.ImportFromWkt(crs.toWkt()) != 0:
            raise ValueError("座標参照系のWKTを読み込めませんでした。")

        csv_path = os.path.splitext(output)[0] + ".csv"
        vrt_path = os.path.splitext(output)[0] + ".vrt"
        point_count = 0
        coordinate_samples = []
        min_x = max_x = min_y = max_y = None

        with self._open_text_source(zip_path, member_name) as raw_file, \
                open(csv_path, "w", encoding="utf-8", newline="") as csv_file:
            import io
            csv_file.write("X,Y,Z\n")
            text_stream = io.TextIOWrapper(raw_file, encoding="utf-8-sig", errors="replace")
            for line_number, line in enumerate(text_stream, start=1):
                columns = line.strip().split(",")
                if not line.strip():
                    continue
                required_column = max(x_column, y_column, z_column)
                if len(columns) <= required_column:
                    raise ValueError(
                        "{}:{}行目に指定列がありません。"
                        .format(display_name, line_number)
                    )
                try:
                    x = float(columns[x_column].strip())
                    y = float(columns[y_column].strip())
                    z = float(columns[z_column].strip())
                except ValueError as error:
                    raise ValueError(
                        "{}:{}行目の数値を読み取れません。".format(
                            display_name, line_number
                        )
                    ) from error
                if not all(math.isfinite(value) for value in (x, y, z)):
                    raise ValueError(
                        "{}:{}行目に無限大またはNaNが含まれています。".format(
                            display_name, line_number
                        )
                    )
                csv_file.write("{:.15g},{:.15g},{:.15g}\n".format(x, y, z))
                point_count += 1
                if min_x is None:
                    min_x = max_x = x
                    min_y = max_y = y
                else:
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)
                    min_y = min(min_y, y)
                    max_y = max(max_y, y)
                if len(coordinate_samples) < 20000:
                    coordinate_samples.append((x, y))
                if progress is not None and point_count % 10000 == 0:
                    progress.setLabelText(
                        "{} を解析中（{}点）".format(display_name, point_count)
                    )
                    QCoreApplication.processEvents()

        if point_count < 3:
            raise ValueError("{} に十分な点がありません。".format(display_name))
        if max_x <= min_x or max_y <= min_y:
            raise ValueError("{} の座標範囲がありません。".format(display_name))

        cell_size = grid_size if grid_size > 0 else self._estimate_cell_size(
            coordinate_samples
        )
        width = max(1, int(math.ceil((max_x - min_x) / cell_size)) + 1)
        height = max(1, int(math.ceil((max_y - min_y) / cell_size)) + 1)
        if width * height > 100000000:
            raise ValueError(
                "グリッド数が1億を超えます。グリッドサイズを大きくしてください。"
            )
        output_bounds = (
            min_x - cell_size / 2.0,
            min_y - cell_size / 2.0,
            max_x + cell_size / 2.0,
            max_y + cell_size / 2.0,
        )

        csv_source = xml_escape(os.path.abspath(csv_path))
        vrt_srs = xml_escape(
            spatial_reference.ExportToWkt()
        )
        with open(vrt_path, "w", encoding="utf-8") as vrt_file:
            vrt_file.write(
                '<OGRVRTDataSource>\n'
                '  <OGRVRTLayer name="points">\n'
                '    <SrcDataSource relativeToVRT="0">{}</SrcDataSource>\n'
                '    <SrcLayer>{}</SrcLayer>\n'
                '    <GeometryType>wkbPoint</GeometryType>\n'
                '    <LayerSRS>{}</LayerSRS>\n'
                '    <GeometryField encoding="PointFromColumns" x="X" y="Y" z="Z"/>\n'
                '  </OGRVRTLayer>\n'
                '</OGRVRTDataSource>\n'.format(
                    csv_source,
                    xml_escape(os.path.splitext(os.path.basename(csv_path))[0]),
                    vrt_srs,
                )
            )

        if progress is not None:
            progress.setLabelText("{} をDEMへ変換中".format(display_name))
            QCoreApplication.processEvents()
        source_dataset = gdal.OpenEx(
            vrt_path,
            gdal.OF_VECTOR,
            open_options=["LIST_ALL_TABLES=YES"],
        )
        if source_dataset is None:
            raise RuntimeError(
                "{} のVRT点データソースを開けませんでした。".format(display_name)
            )
        if source_dataset.GetLayerCount() != 1 or source_dataset.GetLayer(0) is None:
            raise RuntimeError(
                "{} のVRTレイヤを読み込めませんでした。".format(display_name)
            )
        if interpolate_nodata:
            algorithm = "linear:radius=-1:nodata=-9999"
        else:
            algorithm = "nearest:radius1={0}:radius2={0}:angle=0:nodata=-9999".format(
                cell_size * 1.5
            )
        result = gdal.Grid(
            output,
            source_dataset,
            format="GTiff",
            width=width,
            height=height,
            outputBounds=output_bounds,
            outputType=gdal.GDT_Float32,
            algorithm=algorithm,
            zfield="Z",
            creationOptions=["COMPRESS=LZW"],
            layers=["points"],
        )
        if result is None:
            raise RuntimeError("{} のラスター生成に失敗しました。".format(display_name))
        result = None

        raster = gdal.Open(output, gdal.GA_Update)
        if raster is None:
            raise RuntimeError("{} のラスターを開けませんでした。".format(display_name))
        band = raster.GetRasterBand(1)
        values = band.ReadAsArray()
        if interpolate_nodata and values is not None:
            import numpy as np
            values = np.asarray(values)
            no_data_mask = ~np.isfinite(values) | (values == -9999)
            if np.any(no_data_mask):
                from scipy.ndimage import distance_transform_edt
                _, indices = distance_transform_edt(
                    no_data_mask, return_distances=True, return_indices=True
                )
                values[no_data_mask] = values[
                    indices[0][no_data_mask], indices[1][no_data_mask]
                ]
                band.WriteArray(values)
        band.SetNoDataValue(-9999)
        raster = None

        raster = gdal.Open(output, gdal.GA_Update)
        raster.SetProjection(spatial_reference.ExportToWkt())
        raster = None
        for auxiliary_path in (csv_path, vrt_path):
            try:
                os.remove(auxiliary_path)
            except OSError:
                pass

    @contextmanager
    def _open_text_source(self, source_path, member_name):
        if member_name is None:
            with open(source_path, "rb") as raw_file:
                yield raw_file
            return
        with zipfile.ZipFile(source_path) as archive:
            with archive.open(member_name) as raw_file:
                yield raw_file

    def _estimate_cell_size(self, points):
        unique_x = sorted(set(round(point[0], 10) for point in points))
        unique_y = sorted(set(round(point[1], 10) for point in points))
        x_steps = [b - a for a, b in zip(unique_x, unique_x[1:]) if b > a]
        y_steps = [b - a for a, b in zip(unique_y, unique_y[1:]) if b > a]
        steps = x_steps + y_steps
        if not steps:
            raise ValueError("標高点の間隔を推定できませんでした。")
        positive_steps = [step for step in steps if step > 0]
        return min(positive_steps)

    def _merge_rasters(self, rasters, output, crs):
        try:
            from osgeo import gdal, osr
        except ImportError as error:
            raise RuntimeError("QGIS GDALモジュールを読み込めませんでした。") from error

        gdal.UseExceptions()
        spatial_reference = osr.SpatialReference()
        if spatial_reference.ImportFromWkt(crs.toWkt()) != 0:
            raise ValueError("座標参照系のWKTを読み込めませんでした。")
        warp_options = gdal.WarpOptions(
            format="GTiff",
            srcNodata=-9999,
            dstNodata=-9999,
            dstSRS=spatial_reference.ExportToWkt(),
            creationOptions=["COMPRESS=LZW", "BIGTIFF=IF_SAFER"],
        )
        result = gdal.Warp(output, rasters, options=warp_options)
        if result is None:
            raise RuntimeError("標高DEMの結合に失敗しました。")
        result = None
