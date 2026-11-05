-- 数据库提供方测试床：合成的订单库（MySQL 8.0）。全部数据是编造的。
-- 手机号中段固定 0000、证件号地区码 99、邮箱域名 example.invalid、地址是「测试省示例市」，一看就是假的，但能被脱敏正则识别。
-- 账号在 users.sh 里建（密码来自环境变量）。

SET NAMES utf8mb4;
CREATE DATABASE shop DEFAULT CHARACTER SET utf8mb4;
CREATE DATABASE ops DEFAULT CHARACTER SET utf8mb4;
USE shop;

CREATE TABLE customers (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '客户编号',
  customer_name VARCHAR(32) NOT NULL COMMENT '客户姓名',
  phone VARCHAR(16) NOT NULL COMMENT '手机号',
  id_card VARCHAR(18) NOT NULL COMMENT '身份证号',
  email VARCHAR(64) COMMENT '邮箱',
  address VARCHAR(128) COMMENT '常用地址',
  password_hash CHAR(64) NOT NULL COMMENT '登录密码哈希',
  level VARCHAR(8) NOT NULL DEFAULT 'NORMAL' COMMENT '客户等级',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_customers_phone (phone),
  CONSTRAINT chk_customers_level CHECK (level IN ('NORMAL', 'SILVER', 'GOLD'))
) COMMENT = '客户';

CREATE TABLE products (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '商品编号',
  sku VARCHAR(16) NOT NULL COMMENT '商品编码',
  product_name VARCHAR(64) NOT NULL COMMENT '商品名称',
  category VARCHAR(16) NOT NULL COMMENT '品类',
  unit_price DECIMAL(10, 2) NOT NULL COMMENT '单价',
  stock INT NOT NULL DEFAULT 0 COMMENT '库存',
  status VARCHAR(8) NOT NULL DEFAULT 'ON' COMMENT '上架状态',
  PRIMARY KEY (id),
  UNIQUE KEY uk_products_sku (sku),
  CONSTRAINT chk_products_price CHECK (unit_price >= 0)
) COMMENT = '商品';

CREATE TABLE orders (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '订单编号',
  order_no VARCHAR(20) NOT NULL COMMENT '订单号',
  customer_id BIGINT NOT NULL COMMENT '下单客户',
  status VARCHAR(12) NOT NULL DEFAULT 'CREATED' COMMENT '订单状态',
  total_amount DECIMAL(12, 2) NOT NULL DEFAULT 0 COMMENT '订单金额',
  receiver_name VARCHAR(32) COMMENT '收货人',
  receiver_phone VARCHAR(16) COMMENT '收货电话',
  shipping_address VARCHAR(128) COMMENT '收货地址',
  created_at DATETIME NOT NULL COMMENT '下单时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_orders_no (order_no),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id),
  CONSTRAINT chk_orders_status CHECK (status IN ('CREATED', 'PAID', 'SHIPPED', 'DONE', 'CANCELLED'))
) COMMENT = '订单';

CREATE TABLE order_items (
  id BIGINT NOT NULL AUTO_INCREMENT,
  order_id BIGINT NOT NULL COMMENT '所属订单',
  product_id BIGINT NOT NULL COMMENT '商品',
  quantity INT NOT NULL COMMENT '数量',
  unit_price DECIMAL(10, 2) NOT NULL COMMENT '成交单价',
  line_amount DECIMAL(12, 2) NOT NULL DEFAULT 0 COMMENT '行金额，触发器计算',
  PRIMARY KEY (id),
  CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE,
  CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES products (id),
  CONSTRAINT chk_items_quantity CHECK (quantity > 0)
) COMMENT = '订单明细';

CREATE TABLE payments (
  id BIGINT NOT NULL AUTO_INCREMENT,
  order_id BIGINT NOT NULL COMMENT '订单',
  pay_method VARCHAR(12) NOT NULL COMMENT '支付方式',
  amount DECIMAL(12, 2) NOT NULL COMMENT '支付金额',
  card_no VARCHAR(19) COMMENT '银行卡号',
  paid_at DATETIME COMMENT '支付时间',
  status VARCHAR(8) NOT NULL COMMENT '支付状态',
  PRIMARY KEY (id),
  CONSTRAINT fk_payments_order FOREIGN KEY (order_id) REFERENCES orders (id),
  CONSTRAINT chk_payments_amount CHECK (amount >= 0)
) COMMENT = '支付';

CREATE TABLE shipments (
  id BIGINT NOT NULL AUTO_INCREMENT,
  order_id BIGINT NOT NULL COMMENT '订单',
  carrier VARCHAR(16) NOT NULL COMMENT '承运商',
  tracking_no VARCHAR(24) NOT NULL COMMENT '运单号',
  shipped_at DATETIME COMMENT '发货时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_shipments_tracking (tracking_no),
  CONSTRAINT fk_shipments_order FOREIGN KEY (order_id) REFERENCES orders (id)
) COMMENT = '发货';

-- 历史表：有 order_id、customer_id 列但没建外键，用来测推断关系
CREATE TABLE order_notes (
  id BIGINT NOT NULL AUTO_INCREMENT,
  order_id BIGINT NOT NULL COMMENT '订单',
  customer_id BIGINT COMMENT '客户',
  note VARCHAR(200) COMMENT '备注',
  created_at DATETIME NOT NULL,
  PRIMARY KEY (id)
) COMMENT = '订单备注';

CREATE TABLE ops.audit_log (
  id BIGINT NOT NULL AUTO_INCREMENT,
  action VARCHAR(16) NOT NULL,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (id)
) COMMENT = '操作日志';

CREATE TRIGGER trg_order_items_amount BEFORE INSERT ON order_items
  FOR EACH ROW SET NEW.line_amount = NEW.quantity * NEW.unit_price;

CREATE PROCEDURE sp_cancel_order(IN p_order_id BIGINT) COMMENT '取消未支付的订单'
  UPDATE orders SET status = 'CANCELLED' WHERE id = p_order_id AND status = 'CREATED';

CREATE VIEW v_customer_orders AS
  SELECT c.id AS customer_id, c.level, COUNT(o.id) AS order_count, COALESCE(SUM(o.total_amount), 0) AS total_amount
  FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id, c.level;

-- ---- 合成数据 ----

CREATE TEMPORARY TABLE seq (n INT PRIMARY KEY);
INSERT INTO seq (n) WITH RECURSIVE s (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 400) SELECT n FROM s;

INSERT INTO customers (customer_name, phone, id_card, email, address, password_hash, level, created_at)
SELECT CONCAT(ELT(1 + n % 8, '赵', '钱', '孙', '李', '周', '吴', '郑', '王'), '测', n),
       CONCAT('1550000', LPAD(n, 4, '0')),
       CONCAT('990000', 1970 + n % 30, LPAD(1 + n % 12, 2, '0'), LPAD(1 + n % 28, 2, '0'), LPAD(n, 3, '0'), 'X'),
       CONCAT('user', n, '@example.invalid'),
       CONCAT('测试省示例市演示区样例路', n, '号'),
       SHA2(CONCAT('synthetic-password-', n), 256),
       ELT(1 + n % 3, 'NORMAL', 'SILVER', 'GOLD'),
       TIMESTAMP('2026-01-01') + INTERVAL n DAY
FROM seq WHERE n <= 60;

INSERT INTO products (sku, product_name, category, unit_price, stock, status)
SELECT CONCAT('SKU-', LPAD(n, 5, '0')), CONCAT('演示商品', n), ELT(1 + n % 4, '文具', '日用', '食品', '数码'),
       5 + (n * 37) % 500 + 0.99, (n * 13) % 200, IF(n % 10 = 0, 'OFF', 'ON')
FROM seq WHERE n <= 40;

INSERT INTO orders (order_no, customer_id, status, total_amount, receiver_name, receiver_phone, shipping_address, created_at)
SELECT CONCAT('SO', 20260000 + n), 1 + n % 60, ELT(1 + n % 5, 'CREATED', 'PAID', 'SHIPPED', 'DONE', 'CANCELLED'), 0,
       CONCAT(ELT(1 + n % 8, '赵', '钱', '孙', '李', '周', '吴', '郑', '王'), '收', n),
       CONCAT('1560000', LPAD(n, 4, '0')),
       CONCAT('测试省示例市演示区样例路', n % 60, '号'),
       TIMESTAMP('2026-03-01') + INTERVAL n HOUR
FROM seq WHERE n <= 200;

INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT 1 + s.n % 200, p.id, 1 + s.n % 5, p.unit_price
FROM seq s JOIN products p ON p.id = 1 + s.n % 40 WHERE s.n <= 400;

UPDATE orders o JOIN (SELECT order_id, SUM(line_amount) AS total FROM order_items GROUP BY order_id) t
  ON t.order_id = o.id SET o.total_amount = t.total;

INSERT INTO payments (order_id, pay_method, amount, card_no, paid_at, status)
SELECT o.id, ELT(1 + s.n % 3, 'CARD', 'WALLET', 'TRANSFER'), o.total_amount,
       IF(s.n % 3 = 0, CONCAT('6200000000', LPAD(s.n, 6, '0')), NULL),
       o.created_at + INTERVAL 1 HOUR, IF(s.n % 9 = 0, 'REFUND', 'OK')
FROM seq s JOIN orders o ON o.id = s.n WHERE s.n <= 180;

INSERT INTO shipments (order_id, carrier, tracking_no, shipped_at)
SELECT s.n, ELT(1 + s.n % 3, '快递甲', '快递乙', '快递丙'), CONCAT('TRK', LPAD(s.n, 8, '0')),
       TIMESTAMP('2026-03-02') + INTERVAL s.n HOUR
FROM seq s WHERE s.n <= 150;

INSERT INTO order_notes (order_id, customer_id, note, created_at)
SELECT 1 + (n * 7) % 200, 1 + n % 60,
       ELT(1 + n % 4, '请尽快发货', '工作日送达', CONCAT('联系电话 1550000', LPAD(n, 4, '0')), '无'),
       TIMESTAMP('2026-03-05') + INTERVAL n HOUR
FROM seq WHERE n <= 80;

INSERT INTO ops.audit_log (action, created_at)
SELECT ELT(1 + n % 3, 'LOGIN', 'EXPORT', 'LOGOUT'), TIMESTAMP('2026-03-01') + INTERVAL n MINUTE
FROM seq WHERE n <= 30;

DROP TEMPORARY TABLE seq;
ANALYZE TABLE customers, products, orders, order_items, payments, shipments, order_notes, ops.audit_log;
