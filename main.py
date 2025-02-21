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
        self.setGeometry(100, 100, 850, 800)  # 放大視窗大小
        self.setStyleSheet("""
            QWidget {
                background-color: #1A2533;
                color: #D9E6FF;
                font-family: 'Arial';
                font-size: 14px;
            }
            QLineEdit {
                background-color: #2E3B55;
                border: 1px solid #4A90E2;
                border-radius: 6px;
                padding: 8px;
                color: #D9E6FF;
                font-size: 14px;
            }
            QLineEdit:focus {
                border: 2px solid #66B3FF;
                background-color: #3A4A70;
            }
            QLabel {
                font-weight: bold;
                font-size: 16px;
                color: #A3BFFA;
            }
        """)
        layout = QVBoxLayout()
        layout.setSpacing(20)  # 增加垂直間距
        layout.setContentsMargins(30, 30, 30, 30)  # 增加邊距

        # 創建輸入欄位
        self.product_sold = QLineEdit()
        self.total_sold = QLineEdit()
        self.monthly_sales = QLineEdit()
        self.current_inventory = QLineEdit()  # 現有庫存欄位
        self.expected_months = QLineEdit('3')  # 預設值為3

        # 添加提示文字
        self.product_sold.setToolTip("輸入已賣出的商品數量")
        self.total_sold.setToolTip("輸入總共賣出的數量")
        self.monthly_sales.setToolTip("輸入每月的平均銷量")
        self.current_inventory.setToolTip("輸入目前的庫存數量")
        self.expected_months.setToolTip("輸入期望維持的庫存月數")

        # 為每個輸入欄位添加回車鍵事件
        self.product_sold.returnPressed.connect(self.calculate)
        self.total_sold.returnPressed.connect(self.calculate)
        self.monthly_sales.returnPressed.connect(self.calculate)
        self.current_inventory.returnPressed.connect(self.calculate)
        self.expected_months.returnPressed.connect(self.calculate)

        # 創建標籤和輸入欄位
        for label, widget in [
            ('商品賣出數量:', self.product_sold),
            ('賣出總數:', self.total_sold),
            ('月銷量:', self.monthly_sales),
            ('現有庫存:', self.current_inventory),
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
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4A90E2, stop:1 #2E5CB8);
                color: #FFFFFF;
                padding: 12px 20px;
                border: 1px solid #66B3FF;
                border-radius: 10px;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #66B3FF, stop:1 #4A90E2);
                border: 1px solid #80C4FF;
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2E5CB8, stop:1 #1A3D80);
                border: 1px solid #4A90E2;
            }
        """)
        calc_button.setCursor(Qt.PointingHandCursor)
        layout.addWidget(calc_button)

        # 創建清除按鈕
        clear_button = QPushButton('清除')
        clear_button.clicked.connect(self.clear_fields)
        clear_button.setFont(QFont('Arial', 16))
        clear_button.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #5C6E91, stop:1 #3A4A70);
                color: #FFFFFF;
                padding: 12px 20px;
                border: 1px solid #A3BFFA;
                border-radius: 10px;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7388B8, stop:1 #5C6E91);
                border: 1px solid #B0C4FF;
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3A4A70, stop:1 #2E3B55);
                border: 1px solid #7388B8;
            }
        """)
        clear_button.setCursor(Qt.PointingHandCursor)
        layout.addWidget(clear_button)
        
        layout.addSpacing(25)

        # 創建合併的結果顯示標籤
        self.result_label = QLabel('預期庫存: ')
        self.result_label.setFont(QFont('Arial', 16, QFont.Bold))
        self.result_label.setStyleSheet("""
            QLabel {
                background-color: #2E3B55;
                color: #66B3FF;
                padding: 12px;
                border: 1px solid #4A90E2;
                border-radius: 8px;
                font-size: 16px;
            }
        """)
        layout.addWidget(self.result_label)

        self.setLayout(layout)

    def calculate(self):
        try:
            # 檢查是否有空欄位
            if not all([self.product_sold.text(), self.total_sold.text(), 
                       self.monthly_sales.text(), self.current_inventory.text(), 
                       self.expected_months.text()]):
                raise ValueError("所有欄位都必須填寫")
            
            product_sold = float(self.product_sold.text())
            total_sold = float(self.total_sold.text())
            monthly_sales = float(self.monthly_sales.text())
            current_inventory = float(self.current_inventory.text())
            expected_months = float(self.expected_months.text())
            
            # 額外驗證
            if total_sold == 0:
                raise ValueError("賣出總數不能為零")
            if any(x < 0 for x in [product_sold, total_sold, monthly_sales, current_inventory, expected_months]):
                raise ValueError("所有數值必須為正數")
                
            # 計算預期庫存和預期補貨
            expected_inventory = (product_sold / total_sold) * monthly_sales * expected_months
            expected_restock = expected_inventory - current_inventory
            
            self.result_label.setText(f'預期庫存: {expected_inventory:.2f} (補貨: {expected_restock:.2f})')
            self.result_label.setStyleSheet("""
                QLabel {
                    background-color: #2E3B55;
                    color: #66B3FF;
                    padding: 12px;
                    border: 1px solid #4A90E2;
                    border-radius: 8px;
                    font-size: 16px;
                }
            """)
        except ValueError as e:
            self.result_label.setText(f'錯誤：{str(e)}')
            self.result_label.setStyleSheet("""
                QLabel {
                    background-color: #3A2E44;
                    color: #FF6B6B;
                    padding: 12px;
                    border: 1px solid #FF6B6B;
                    border-radius: 8px;
                    font-size: 16px;
                }
            """)

    def clear_fields(self):
        self.product_sold.clear()
        self.total_sold.clear()
        self.monthly_sales.clear()
        self.current_inventory.clear()
        self.expected_months.setText('3')
        self.result_label.setText('預期庫存: ')
        self.result_label.setStyleSheet("""
            QLabel {
                background-color: #2E3B55;
                color: #66B3FF;
                padding: 12px;
                border: 1px solid #4A90E2;
                border-radius: 8px;
                font-size: 16px;
            }
        """)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    calculator = SalesCalculator()
    calculator.show()
    sys.exit(app.exec_())