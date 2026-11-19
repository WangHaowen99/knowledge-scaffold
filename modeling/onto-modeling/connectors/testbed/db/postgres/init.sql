-- 数据库提供方测试床：合成的订单库（PostgreSQL 16，库 shopdb，schema shop / ops）。全部数据是编造的。
-- 手机号中段固定 0000、证件号地区码 99、邮箱域名 example.invalid、地址是「测试省示例市」，一看就是假的，但能被脱敏正则识别。
-- 账号在 users.sh 里建（密码来自环境变量）。

CREATE SCHEMA shop;
CREATE SCHEMA ops;

CREATE TABLE shop.customers (
  id BIGINT GENERATED ALWAYS AS IDENTITY,
  customer_name VARCHAR(32) NOT NULL,
  phone VARCHAR(16) NOT NULL,
  id_card VARCHAR(18) NOT NULL,
  email VARCHAR(64),
  address VARCHAR(128),
  password_hash CHAR(64) NOT NULL,
  level VARCHAR(8) NOT NULL DEFAULT 'NORMAL',
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT pk_customers PRIMARY KEY (id),
  CONSTRAINT uk_customers_phone UNIQUE (phone),
  CONSTRAINT chk_customers_level CHECK (level IN ('NORMAL', 'SILVER', 'GOLD'))
);
COMMENT ON TABLE shop.customers IS '客户';
COMMENT ON COLUMN shop.customers.id IS '客户编号';
COMMENT ON COLUMN shop.customers.customer_name IS '客户姓名';
COMMENT ON COLUMN shop.customers.phone IS '手机号';
COMMENT ON COLUMN shop.customers.id_card IS '身份证号';
COMMENT ON COLUMN shop.customers.email IS '邮箱';
COMMENT ON COLUMN shop.customers.address IS '常用地址';
COMMENT ON COLUMN shop.customers.password_hash IS '登录密码哈希';
COMMENT ON COLUMN shop.customers.level IS '客户等级';
COMMENT ON COLUMN shop.customers.created_at IS '注册时间';

CREATE TABLE shop.products (
  id BIGINT GENERATED ALWAYS AS IDENTITY,
  sku VARCHAR(16) NOT NULL,
  product_name VARCHAR(64) NOT NULL,
  category VARCHAR(16) NOT NULL,
  unit_price NUMERIC(10, 2) NOT NULL,
  stock INTEGER NOT NULL DEFAULT 0,
  status VARCHAR(8) NOT NULL DEFAULT 'ON',
  CONSTRAINT pk_products PRIMARY KEY (id),
  CONSTRAINT uk_products_sku UNIQUE (sku),
  CONSTRAINT chk_products_price CHECK (unit_price >= 0)
);
COMMENT ON TABLE shop.products IS '商品';
COMMENT ON COLUMN shop.products.sku IS '商品编码';
COMMENT ON COLUMN shop.products.product_name IS '商品名称';
COMMENT ON COLUMN shop.products.unit_price IS '单价';

CREATE TABLE shop.orders (
  id BIGINT GENERATED ALWAYS AS IDENTITY,
  order_no VARCHAR(20) NOT NULL,
  customer_id BIGINT NOT NULL,
  status VARCHAR(12) NOT NULL DEFAULT 'CREATED',
  total_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
  receiver_name VARCHAR(32),
  receiver_phone VARCHAR(16),
  shipping_address VARCHAR(128),
  created_at TIMESTAMP NOT NULL,
  CONSTRAINT pk_orders PRIMARY KEY (id),
  CONSTRAINT uk_orders_no UNIQUE (order_no),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES shop.customers (id),
  CONSTRAINT chk_orders_status CHECK (status IN ('CREATED', 'PAID', 'SHIPPED', 'DONE', 'CANCELLED'))
);
COMMENT ON TABLE shop.orders IS '订单';
COMMENT ON COLUMN shop.orders.total_amount IS '订单金额';
COMMENT ON COLUMN shop.orders.receiver_name IS '收货人';
COMMENT ON COLUMN shop.orders.receiver_phone IS '收货电话';
COMMENT ON COLUMN shop.orders.shipping_address IS '收货地址';

CREATE TABLE shop.order_items (
  id BIGINT GENERATED ALWAYS AS IDENTITY,
  order_id BIGINT NOT NULL,
  product_id BIGINT NOT NULL,
  quantity INTEGER NOT NULL,
  unit_price NUMERIC(10, 2) NOT NULL,
  line_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
  CONSTRAINT pk_order_items PRIMARY KEY (id),
  CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES shop.orders (id) ON DELETE CASCADE,
  CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES shop.products (id),
  CONSTRAINT chk_items_quantity CHECK (quantity > 0)
);
COMMENT ON TABLE shop.order_items IS '订单明细';
COMMENT ON COLUMN shop.order_items.line_amount IS '行金额，触发器计算';

CREATE TABLE shop.payments (
  id BIGINT GENERATED ALWAYS AS IDENTITY,
  order_id BIGINT NOT NULL,
  pay_method VARCHAR(12) NOT NULL,
  amount NUMERIC(12, 2) NOT NULL,
  card_no VARCHAR(19),
  paid_at TIMESTAMP,
  status VARCHAR(8) NOT NULL,
  CONSTRAINT pk_payments PRIMARY KEY (id),
  CONSTRAINT fk_payments_order FOREIGN KEY (order_id) REFERENCES shop.orders (id),
  CONSTRAINT chk_payments_amount CHECK (amount >= 0)
);
COMMENT ON TABLE shop.payments IS '支付';
COMMENT ON COLUMN shop.payments.card_no IS '银行卡号';

CREATE TABLE shop.shipments (
  id BIGINT GENERATED ALWAYS AS IDENTITY,
  order_id BIGINT NOT NULL,
  carrier VARCHAR(16) NOT NULL,
  tracking_no VARCHAR(24) NOT NULL,
  shipped_at TIMESTAMP,
  CONSTRAINT pk_shipments PRIMARY KEY (id),
  CONSTRAINT uk_shipments_tracking UNIQUE (tracking_no),
  CONSTRAINT fk_shipments_order FOREIGN KEY (order_id) REFERENCES shop.orders (id)
);
COMMENT ON TABLE shop.shipments IS '发货';

-- 历史表：有 order_id、customer_id 列但没建外键，用来测推断关系
CREATE TABLE shop.order_notes (
  id BIGINT GENERATED ALWAYS AS IDENTITY,
  order_id BIGINT NOT NULL,
  customer_id BIGINT,
  note VARCHAR(200),
  created_at TIMESTAMP NOT NULL,
  CONSTRAINT pk_order_notes PRIMARY KEY (id)
);
COMMENT ON TABLE shop.order_notes IS '订单备注';

CREATE TABLE ops.audit_log (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  action VARCHAR(16) NOT NULL,
  created_at TIMESTAMP NOT NULL
);
COMMENT ON TABLE ops.audit_log IS '操作日志';

CREATE FUNCTION shop.fn_order_items_amount() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.line_amount := NEW.quantity * NEW.unit_price;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_order_items_amount BEFORE INSERT ON shop.order_items
  FOR EACH ROW EXECUTE FUNCTION shop.fn_order_items_amount();

CREATE PROCEDURE shop.sp_cancel_order(p_order_id BIGINT) LANGUAGE sql AS $$
  UPDATE shop.orders SET status = 'CANCELLED' WHERE id = p_order_id AND status = 'CREATED'
$$;
COMMENT ON PROCEDURE shop.sp_cancel_order(BIGINT) IS '取消未支付的订单';

CREATE VIEW shop.v_customer_orders AS
  SELECT c.id AS customer_id, c.level, count(o.id) AS order_count, coalesce(sum(o.total_amount), 0) AS total_amount
  FROM shop.customers c LEFT JOIN shop.orders o ON o.customer_id = c.id GROUP BY c.id, c.level;

-- ---- 合成数据 ----

INSERT INTO shop.customers (customer_name, phone, id_card, email, address, password_hash, level, created_at)
SELECT (ARRAY['赵', '钱', '孙', '李', '周', '吴', '郑', '王'])[1 + n % 8] || '测' || n,
       '1550000' || lpad(n::text, 4, '0'),
       '990000' || (1970 + n % 30) || lpad((1 + n % 12)::text, 2, '0') || lpad((1 + n % 28)::text, 2, '0')
         || lpad(n::text, 3, '0') || 'X',
       'user' || n || '@example.invalid',
       '测试省示例市演示区样例路' || n || '号',
       encode(sha256(convert_to('synthetic-password-' || n, 'UTF8')), 'hex'),
       (ARRAY['NORMAL', 'SILVER', 'GOLD'])[1 + n % 3],
       TIMESTAMP '2026-01-01' + n * INTERVAL '1 day'
FROM generate_series(1, 60) AS n;

INSERT INTO shop.products (sku, product_name, category, unit_price, stock, status)
SELECT 'SKU-' || lpad(n::text, 5, '0'), '演示商品' || n, (ARRAY['文具', '日用', '食品', '数码'])[1 + n % 4],
       5 + (n * 37) % 500 + 0.99, (n * 13) % 200, CASE WHEN n % 10 = 0 THEN 'OFF' ELSE 'ON' END
FROM generate_series(1, 40) AS n;

INSERT INTO shop.orders (order_no, customer_id, status, total_amount, receiver_name, receiver_phone, shipping_address,
                         created_at)
SELECT 'SO' || (20260000 + n), 1 + n % 60, (ARRAY['CREATED', 'PAID', 'SHIPPED', 'DONE', 'CANCELLED'])[1 + n % 5], 0,
       (ARRAY['赵', '钱', '孙', '李', '周', '吴', '郑', '王'])[1 + n % 8] || '收' || n,
       '1560000' || lpad(n::text, 4, '0'),
       '测试省示例市演示区样例路' || (n % 60) || '号',
       TIMESTAMP '2026-03-01' + n * INTERVAL '1 hour'
FROM generate_series(1, 200) AS n;

INSERT INTO shop.order_items (order_id, product_id, quantity, unit_price)
SELECT 1 + n % 200, p.id, 1 + n % 5, p.unit_price
FROM generate_series(1, 400) AS n JOIN shop.products p ON p.id = 1 + n % 40;

UPDATE shop.orders o SET total_amount = t.total
FROM (SELECT order_id, sum(line_amount) AS total FROM shop.order_items GROUP BY order_id) t WHERE t.order_id = o.id;

INSERT INTO shop.payments (order_id, pay_method, amount, card_no, paid_at, status)
SELECT o.id, (ARRAY['CARD', 'WALLET', 'TRANSFER'])[1 + n % 3], o.total_amount,
       CASE WHEN n % 3 = 0 THEN '6200000000' || lpad(n::text, 6, '0') END,
       o.created_at + INTERVAL '1 hour', CASE WHEN n % 9 = 0 THEN 'REFUND' ELSE 'OK' END
FROM generate_series(1, 180) AS n JOIN shop.orders o ON o.id = n;

INSERT INTO shop.shipments (order_id, carrier, tracking_no, shipped_at)
SELECT n, (ARRAY['快递甲', '快递乙', '快递丙'])[1 + n % 3], 'TRK' || lpad(n::text, 8, '0'),
       TIMESTAMP '2026-03-02' + n * INTERVAL '1 hour'
FROM generate_series(1, 150) AS n;

INSERT INTO shop.order_notes (order_id, customer_id, note, created_at)
SELECT 1 + (n * 7) % 200, 1 + n % 60,
       (ARRAY['请尽快发货', '工作日送达', '联系电话 1550000' || lpad(n::text, 4, '0'), '无'])[1 + n % 4],
       TIMESTAMP '2026-03-05' + n * INTERVAL '1 hour'
FROM generate_series(1, 80) AS n;

INSERT INTO ops.audit_log (action, created_at)
SELECT (ARRAY['LOGIN', 'EXPORT', 'LOGOUT'])[1 + n % 3], TIMESTAMP '2026-03-01' + n * INTERVAL '1 minute'
FROM generate_series(1, 30) AS n;

ANALYZE;
