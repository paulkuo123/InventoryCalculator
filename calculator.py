import sys
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
                             QPushButton, QGraphicsDropShadowEffect, QGridLayout, QFrame, 
                             QSizePolicy, QSpacerItem, QProgressBar, QShortcut, QInputDialog)
from PyQt5.QtGui import QFont, QPalette, QColor, QIcon, QFontDatabase, QValidator, QKeySequence
from PyQt5.QtCore import Qt, QPropertyAnimation, QEasingCurve, QSize, QPoint

# 定義深色主題樣式
dark_theme_stylesheet = """
    QWidget {
        background-color: #121826;
        color: #E6F0FF;
        font-family: 'Roboto', 'Arial';
        font-size: 14px;
    }
    QLineEdit {
        background-color: #1E2739;
        border: 1px solid #2E3B55;
        border-radius: 8px;
        padding: 10px 15px;
        color: #E6F0FF;
        font-size: 15px;
        selection-color: #FFFFFF;
        selection-background-color: #3274d9;
    }
    QLineEdit:focus {
        border: 2px solid #3274d9;
        background-color: #283347;
    }
    QLineEdit::placeholder {
        color: #4A5568;
    }
    QLabel {
        font-weight: bold;
        font-size: 16px;
        color: #B0C4FF;
    }
    QFrame#card {
        background-color: #1A2334;
        border-radius: 12px;
        border: 1px solid #2D3648;
    }
"""

class NumberValidator(QValidator):
    def validate(self, input_str, pos):
        if not input_str or input_str.replace('.', '').isdigit():
            return (QValidator.Acceptable, input_str, pos)
        return (QValidator.Invalid, input_str, pos)

class HoverButton(QPushButton):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setMouseTracking(True)
        self.original_text = text
        self.original_geometry = None
        
        # 設置動畫效果
        self.animation = QPropertyAnimation(self, b"geometry")
        self.animation.setDuration(100)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        
        # 添加陰影效果
        self.shadow = QGraphicsDropShadowEffect()
        self.shadow.setBlurRadius(15)
        self.shadow.setColor(QColor(0, 0, 0, 80))
        self.shadow.setOffset(0, 5)
        self.setGraphicsEffect(self.shadow)
        
    def enterEvent(self, event):
        self.shadow.setBlurRadius(25)
        self.shadow.setColor(QColor(0, 0, 0, 120))
        if self.original_geometry is None:
            self.original_geometry = self.geometry()
        self.animation.setEndValue(self.original_geometry.adjusted(-2, -2, 2, 2))  # 輕微放大
        self.animation.start()
        
    def leaveEvent(self, event):
        self.shadow.setBlurRadius(15)
        self.shadow.setColor(QColor(0, 0, 0, 80))
        if self.original_geometry is not None:
            self.animation.setEndValue(self.original_geometry)  # 恢復原始大小
            self.animation.start()

class ModernLineEdit(QLineEdit):
    def __init__(self, placeholder="", parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setMinimumHeight(45)
        
        # 添加陰影效果
        self.shadow = QGraphicsDropShadowEffect()
        self.shadow.setBlurRadius(10)
        self.shadow.setColor(QColor(0, 0, 0, 50))
        self.shadow.setOffset(0, 2)
        self.setGraphicsEffect(self.shadow)
        
    def focusInEvent(self, event):
        self.shadow.setBlurRadius(15)
        self.shadow.setColor(QColor(74, 144, 226, 100))
        super().focusInEvent(event)
        
    def focusOutEvent(self, event):
        self.shadow.setBlurRadius(10)
        self.shadow.setColor(QColor(0, 0, 0, 50))
        super().focusOutEvent(event)

class InventoryCalculator:
    @staticmethod
    def calculate_inventory(product_sold, total_sold, monthly_sales, current_inventory, expected_months):
        if total_sold == 0:
            raise ValueError("賣出總數不能為零")
        expected_inventory = (product_sold / total_sold) * monthly_sales * expected_months
        restock = expected_inventory - current_inventory
        return expected_inventory, restock

class SalesCalculator(QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()
        
    def initUI(self):
        self.setWindowTitle('智能庫存預測系統')
        self.setMinimumSize(600, 700)
        self.setGeometry(100, 100, 900, 850)
        
        # 加載字體（可選，若無字體則移除）
        # QFontDatabase.addApplicationFont(":/fonts/Roboto-Regular.ttf")
        # QFontDatabase.addApplicationFont(":/fonts/Roboto-Bold.ttf")
        
        # 使用深色主題
        self.setStyleSheet(dark_theme_stylesheet)
        
        # 主佈局
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(30, 30, 30, 30)
        main_layout.setSpacing(25)
        
        # 標題部分
        title_layout = QHBoxLayout()
        app_title = QLabel("莉莉安庫存系統")
        app_title.setFont(QFont('Arial', 24, QFont.Bold))  # 若無 Roboto，使用 Arial
        app_title.setStyleSheet("color: #4A90E2; margin-bottom: 5px;")
        title_layout.addWidget(app_title)
        title_layout.addStretch()
        
        # 創建卡片式容器
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(25, 25, 25, 25)
        card_layout.setSpacing(20)
        
        # 卡片陰影效果
        card_shadow = QGraphicsDropShadowEffect()
        card_shadow.setBlurRadius(20)
        card_shadow.setColor(QColor(0, 0, 0, 80))
        card_shadow.setOffset(0, 10)
        card.setGraphicsEffect(card_shadow)
        
        # 輸入欄位使用網格佈局
        input_layout = QGridLayout()
        input_layout.setVerticalSpacing(20)
        input_layout.setHorizontalSpacing(15)
        
        # 創建現代風格的輸入欄位
        self.product_sold = ModernLineEdit("輸入已賣出的商品數量 (單位：個)")
        self.total_sold = ModernLineEdit("輸入總共賣出的數量 (單位：個)")
        self.monthly_sales = ModernLineEdit("輸入每月的平均銷量 (單位：個)")
        self.current_inventory = ModernLineEdit("輸入目前的庫存數量 (單位：個)")
        self.expected_months = ModernLineEdit("輸入期望維持的庫存月數 (單位：月)")
        self.expected_months.setText('4')
        
        # 添加輸入驗證和工具提示
        validator = NumberValidator()
        for widget in [self.product_sold, self.total_sold, self.monthly_sales, self.current_inventory, self.expected_months]:
            widget.setValidator(validator)
            widget.setToolTip(f"{widget.placeholderText()}, 需為正數")
        
        # 添加標籤和輸入欄位到網格佈局
        labels = ['商品賣出數量', '賣出總數', '月銷量', '現有庫存', '預期庫存月數']
        inputs = [self.product_sold, self.total_sold, self.monthly_sales, self.current_inventory, self.expected_months]
        icons = ["📊", "📈", "📉", "🗃️", "📆"]
        
        for i, (label_text, input_widget) in enumerate(zip(labels, inputs)):
            label = QLabel(f"{label_text}:")
            label.setFixedWidth(150)
            icon_label = QLabel(icons[i])
            icon_label.setFont(QFont('Segoe UI Emoji', 16))
            icon_label.setFixedWidth(30)
            row_layout = QHBoxLayout()
            row_layout.addWidget(icon_label)
            row_layout.addWidget(label)
            row_layout.addWidget(input_widget)
            input_layout.addLayout(row_layout, i, 0)
        
        card_layout.addLayout(input_layout)
        
        # 按鈕區域，使用水平佈局
        button_layout = QHBoxLayout()
        button_layout.setSpacing(15)
        
        # 計算按鈕
        self.calc_button = HoverButton('計 算 預 期 庫 存')  # 改為 self.calc_button 以便後續綁定
        self.calc_button.clicked.connect(self.calculate)
        self.calc_button.setFont(QFont('Arial', 16, QFont.Bold))
        self.calc_button.setMinimumHeight(55)
        self.calc_button.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                                           stop:0 #3274d9, stop:1 #1a4fa0);
                color: #FFFFFF;
                padding: 12px 25px;
                border: none;
                border-radius: 8px;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                                          stop:0 #4285F4, stop:1 #2563EB);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                                          stop:0 #1a4fa0, stop:1 #0d2b5e);
            }
        """)
        self.calc_button.setCursor(Qt.PointingHandCursor)
        self.calc_button.setToolTip("點擊計算預期庫存和補貨量")
        
        # 清除按鈕
        clear_button = HoverButton('清 除')
        clear_button.clicked.connect(self.clear_fields)
        clear_button.setFont(QFont('Arial', 16, QFont.Bold))
        clear_button.setMinimumHeight(55)
        clear_button.setStyleSheet("""
            QPushButton {
                background-color: #2E3B55;
                color: #D9E6FF;
                padding: 12px 25px;
                border: none;
                border-radius: 8px;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3A4A70;
            }
            QPushButton:pressed {
                background-color: #1E2739;
            }
        """)
        clear_button.setCursor(Qt.PointingHandCursor)
        clear_button.setToolTip("清除所有輸入欄位")
        
        # 添加按鈕到佈局
        button_layout.addWidget(self.calc_button, 2)
        button_layout.addWidget(clear_button, 1)
        card_layout.addLayout(button_layout)
        
        # 創建結果顯示卡片
        result_card = QFrame()
        result_card.setObjectName("card")
        result_layout = QVBoxLayout(result_card)
        result_layout.setContentsMargins(20, 20, 20, 20)
        
        # 結果標籤
        result_title = QLabel("分析結果")
        result_title.setFont(QFont('Arial', 16, QFont.Bold))
        result_title.setStyleSheet("color: #4A90E2;")
        result_layout.addWidget(result_title)
        
        # 創建結果顯示標籤
        self.result_label = QLabel('等待計算...')
        self.result_label.setFont(QFont('Arial', 16))
        self.result_label.setStyleSheet("""
            QLabel {
                background-color: #1E2739;
                color: #66B3FF;
                padding: 15px;
                border-radius: 8px;
                font-size: 16px;
                border-left: 4px solid #3274d9;
            }
        """)
        self.result_label.setAlignment(Qt.AlignCenter)
        self.result_label.setWordWrap(True)
        result_layout.addWidget(self.result_label)
        
        # 添加庫存進度條
        self.inventory_bar = QProgressBar()
        self.inventory_bar.setMaximum(100)
        self.inventory_bar.setValue(0)
        result_layout.addWidget(self.inventory_bar)
        
        # 結果卡片陰影
        result_shadow = QGraphicsDropShadowEffect()
        result_shadow.setBlurRadius(20)
        result_shadow.setColor(QColor(0, 0, 0, 80))
        result_shadow.setOffset(0, 10)
        result_card.setGraphicsEffect(result_shadow)
        
        # 添加所有元素到主佈局
        main_layout.addLayout(title_layout)
        main_layout.addWidget(card)
        main_layout.addWidget(result_card)
        main_layout.addStretch()  # 添加彈性空間，使內容靠上
        
        self.setLayout(main_layout)
        
        # 添加快捷鍵
        QShortcut(QKeySequence("Ctrl+Enter"), self, self.calculate)
        QShortcut(QKeySequence("Esc"), self, self.clear_fields)
        QShortcut(QKeySequence("Return"), self, self.calc_button.click)  # Enter 鍵觸發計算按鈕
        
    def calculate(self):
        try:
            if not all([self.product_sold.text(), self.total_sold.text(), 
                       self.monthly_sales.text(), self.current_inventory.text(), 
                       self.expected_months.text()]):
                raise ValueError("所有欄位都必須填寫")
            
            product_sold = float(self.product_sold.text())
            total_sold = float(self.total_sold.text())
            monthly_sales = float(self.monthly_sales.text())
            current_inventory = float(self.current_inventory.text())
            expected_months = float(self.expected_months.text())
            
            if any(x < 0 for x in [product_sold, total_sold, monthly_sales, current_inventory, expected_months]):
                raise ValueError("所有數值必須為正數")
            
            # 特殊情況：當庫存為0且月銷量也為0時，使用歷史佔比法
            if current_inventory == 0 and monthly_sales == 0:
                if total_sold == 0:
                    raise ValueError("賣出總數不能為零")
                
                # 計算歷史佔比
                ratio = product_sold / total_sold
                
                # 彈出輸入框詢問商品整體總月銷量
                total_monthly_sales, ok = QInputDialog.getDouble(
                    self, "輸入數據", "檢測到庫存與月銷量皆為0，請輸入商品整體總月銷量：", 
                    value=0, min=0, decimals=2
                )
                
                if ok:
                    # 預估月銷量 = 該商品總月銷量 × 歷史佔比
                    estimated_monthly_sales = total_monthly_sales * ratio
                    # 補貨量 = 預估月銷量 × 預期庫存水位月數 - 當前庫存(0)
                    expected_inventory = estimated_monthly_sales * expected_months
                    restock = expected_inventory  # 因為 current_inventory 為 0
                else:
                    return  # 使用者取消輸入
            else:
                expected_inventory, restock = InventoryCalculator.calculate_inventory(
                    product_sold, total_sold, monthly_sales, current_inventory, expected_months
                )
            
            self.result_label.setText(f'預期庫存: {expected_inventory:.2f} 單位 | 建議補貨: {restock:.2f} 單位')
            if restock <= 0:
                self.result_label.setStyleSheet("""
                    QLabel {
                        background-color: #132C1E;
                        color: #4ADE80;
                        padding: 15px;
                        border-radius: 8px;
                        font-size: 16px;
                        border-left: 4px solid #22C55E;
                    }
                """)
            else:
                self.result_label.setStyleSheet("""
                    QLabel {
                        background-color: #1E2739;
                        color: #3B82F6;
                        padding: 15px;
                        border-radius: 8px;
                        font-size: 16px;
                        border-left: 4px solid #3274d9;
                    }
                """)
            
            self.inventory_bar.setValue(min(100, int((current_inventory / expected_inventory) * 100)))
            
        except ValueError as e:
            self.result_label.setText(f'錯誤：{str(e)}')
            self.result_label.setStyleSheet("""
                QLabel {
                    background-color: #2D1A22;
                    color: #F87171;
                    padding: 15px;
                    border-radius: 8px;
                    font-size: 16px;
                    border-left: 4px solid #EF4444;
                }
            """)
            
    def clear_fields(self):
        self.product_sold.clear()
        self.total_sold.clear()
        self.monthly_sales.clear()
        self.current_inventory.clear()
        self.expected_months.setText('4')
        self.result_label.setText('等待計算...')
        self.result_label.setStyleSheet("""
            QLabel {
                background-color: #1E2739;
                color: #66B3FF;
                padding: 15px;
                border-radius: 8px;
                font-size: 16px;
                border-left: 4px solid #3274d9;
            }
        """)
        self.inventory_bar.setValue(0)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    calculator = SalesCalculator()
    calculator.show()
    sys.exit(app.exec_())