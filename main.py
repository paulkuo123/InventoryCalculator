import sys
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton
from PyQt5.QtGui import QFont, QPalette, QColor
from PyQt5.QtCore import Qt

class SalesCalculator(QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()

    def initUI(self):
        self.setWindowTitle('庫存計算機')
        self.setGeometry(100, 100, 500, 400)  # 設置初始窗口大小
        self.setStyleSheet("""
            QWidget {
                background-color: #E6F3FF;
                color: #00008B;
                font-size: 14px;
            }
            QLineEdit {
                background-color: white;
                border: 1px solid #4682B4;
                border-radius: 5px;
                padding: 5px;
                font-size: 14px;
            }
            QLabel {
                font-weight: bold;
                font-size: 16px;
            }
        """)
        layout = QVBoxLayout()
        layout.setSpacing(15)  # 增加垂直間距
        layout.setContentsMargins(20, 20, 20, 20)  # 增加邊距

        # 創建輸入欄位
        self.product_sold = QLineEdit()
        self.total_sold = QLineEdit()
        self.monthly_sales = QLineEdit()
        self.expected_months = QLineEdit('3')  # 新欄位，預設值為3

        # 為每個輸入欄位添加回車鍵事件
        self.product_sold.returnPressed.connect(self.calculate)
        self.total_sold.returnPressed.connect(self.calculate)
        self.monthly_sales.returnPressed.connect(self.calculate)
        self.expected_months.returnPressed.connect(self.calculate)

        # 創建標籤和輸入欄位
        for label, widget in [
            ('商品賣出數量:', self.product_sold),
            ('賣出總數:', self.total_sold),
            ('月銷量:', self.monthly_sales),
            ('預期庫存月數:', self.expected_months)
        ]:
            hbox = QHBoxLayout()
            label_widget = QLabel(label)
            label_widget.setFixedWidth(150)  # 固定標籤寬度
            hbox.addWidget(label_widget)
            hbox.addWidget(widget)
            layout.addLayout(hbox)

        # 創建計算按鈕
        calc_button = QPushButton('計算預期庫存')
        calc_button.clicked.connect(self.calculate)
        calc_button.setFont(QFont('Arial', 16))
        calc_button.setStyleSheet("""
            QPushButton {
                background-color: #4682B4;
                color: white;
                padding: 15px 25px;
                border: none;
                border-radius: 8px;
                font-size: 16px;
            }
            QPushButton:hover {
                background-color: #5c9ccc;
            }
            QPushButton:pressed {
                background-color: #3a6e9e;
            }
        """)
        calc_button.setCursor(Qt.PointingHandCursor)
        layout.addWidget(calc_button)
        layout.addSpacing(20)

        # 創建結果顯示標籤
        self.result_label = QLabel('預期庫存: ')
        self.result_label.setFont(QFont('Arial', 16, QFont.Bold))
        self.result_label.setStyleSheet("""
            QLabel {
                background-color: #B0E0FF;
                color: #00008B;
                padding: 10px;
                border: 1px solid #4682B4;
                border-radius: 5px;
            }
        """)
        layout.addWidget(self.result_label)

        self.setLayout(layout)

    def calculate(self):
        try:
            product_sold = float(self.product_sold.text())
            total_sold = float(self.total_sold.text())
            monthly_sales = float(self.monthly_sales.text())
            expected_months = float(self.expected_months.text())

            expected_inventory = (product_sold / total_sold) * monthly_sales * expected_months
            self.result_label.setText(f'預期庫存: {expected_inventory:.2f}')
            self.result_label.setStyleSheet("""
                QLabel {
                    background-color: #B0E0FF;
                    color: #00008B;
                    padding: 10px;
                    border: 1px solid #4682B4;
                    border-radius: 5px;
                    font-size: 16px;
                }
            """)
        except ValueError:
            self.result_label.setText('錯誤：請確保所有欄位都填寫了有效的數字')
            self.result_label.setStyleSheet("""
                QLabel {
                    background-color: #FFD2D2;
                    color: #8B0000;
                    padding: 10px;
                    border: 1px solid #8B0000;
                    border-radius: 5px;
                    font-size: 16px;
                }
            """)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    calculator = SalesCalculator()
    calculator.show()
    sys.exit(app.exec_())