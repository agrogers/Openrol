# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from datetime import timedelta, date
from odoo import api, fields, models, _
from odoo.tools import float_compare, format_datetime, format_time
from pytz import timezone, UTC


class EnrollmentContract(models.Model):
    _inherit = 'sale.order'

    is_enrollment_order = fields.Boolean("Created In App Enrollment")
    enroll_status = fields.Selection([
        ('draft', 'Draft'),
        ('pending', 'Pending'),
        ('confirm', 'Enrolled'),
        ('drop', 'Dropped'),
        ('cancel', 'Cancelled'),
    ], string="Enroll Status", default='draft',compute='_compute_enrollment_status', store=True)
    # enroll_status = next action to do basically, but shown string is action done.

    has_pickable_lines = fields.Boolean(compute="_compute_enrollment_status", store=True)
    has_returnable_lines = fields.Boolean(compute="_compute_enrollment_status", store=True)
    next_action_date = fields.Datetime(
        string="Next Action", compute='_compute_enrollment_status', store=True)

    has_late_lines = fields.Boolean(compute="_compute_has_late_lines")
    
    # Academic Fields
    course_id = fields.Many2one('oe.school.course', string='Course')
    batch_id = fields.Many2one('oe.school.course.batch', string='Batch')
    
    sale_order_fees_template_id = fields.Many2one('sale.order.fees.template', 'Fees Template', readonly=True, check_company=True, states={'draft': [('readonly', False)], 'sent': [('readonly', False)]}, domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]")

    
    def _compute_line_data_for_template_change(self, line):
        return {
            'sequence': line.sequence,
            'display_type': line.display_type,
            'name': line.name,
            'state': 'draft',
        }
    
    @api.onchange('sale_order_fees_template_id')
    def onchange_sale_order_fees_template_id(self):

        if not self.sale_order_fees_template_id:
            self.require_signature = self._get_default_require_signature()
            self.require_payment = self._get_default_require_payment()
            return

        template = self.sale_order_fees_template_id.with_context(lang=self.partner_id.lang)

        # --- first, process the list of products from the template
        order_lines = [(5, 0, 0)]
        for line in template.sale_order_fees_template_line_ids:
            data = self._compute_line_data_for_template_change(line)

            if line.product_id:
                price = line.product_id.lst_price
                discount = 0

                if self.pricelist_id:
                    pricelist_price = self.pricelist_id.with_context(uom=line.product_uom_id.id).get_product_price(line.product_id, 1, False)

                    if self.pricelist_id.discount_policy == 'without_discount' and price:
                        discount = max(0, (price - pricelist_price) * 100 / price)
                    else:
                        price = pricelist_price

                data.update({
                    'price_unit': price,
                    'discount': discount,
                    'product_uom_qty': line.product_uom_qty,
                    'product_id': line.product_id.id,
                    'product_uom': line.product_uom_id.id,
                    'customer_lead': self._get_customer_lead(line.product_id.product_tmpl_id),
                })

            order_lines.append((0, 0, data))

        self.order_line = order_lines
        self.order_line._compute_tax_id()

        self.require_signature = template.require_signature
        self.require_payment = template.require_payment

        #if not is_html_empty(template.note):
        #    self.note = template.note

            
    @api.depends('is_enrollment_order', 'next_action_date', 'enroll_status')
    def _compute_has_late_lines(self):
        for order in self:
            order.has_late_lines = (
                order.is_enrollment_order
                and order.enroll_status in ['pickup', 'return']  # has_pickable_lines or has_returnable_lines
                and order.next_action_date < fields.Datetime.now())

    @api.depends('state', 'order_line', 'order_line.product_uom_qty', 'order_line.qty_delivered', 'order_line.qty_returned')
    def _compute_enrollment_status(self):
        # TODO replace multiple assignations by one write?
        for order in self:
            if order.state in ['sale', 'done'] and order.is_enrollment_order:
                rental_order_lines = order.order_line.filtered('is_enrollment')
                pickeable_lines = rental_order_lines.filtered(lambda sol: sol.qty_delivered < sol.product_uom_qty)
                returnable_lines = rental_order_lines.filtered(lambda sol: sol.qty_returned < sol.qty_delivered)
                min_pickup_date = min(pickeable_lines.mapped('pickup_date')) if pickeable_lines else 0
                min_return_date = min(returnable_lines.mapped('return_date')) if returnable_lines else 0
                if pickeable_lines and (not returnable_lines or min_pickup_date <= min_return_date):
                    order.enroll_status = 'pickup'
                    order.next_action_date = min_pickup_date
                elif returnable_lines:
                    order.enroll_status = 'return'
                    order.next_action_date = min_return_date
                else:
                    order.enroll_status = 'returned'
                    order.next_action_date = False
                order.has_pickable_lines = bool(pickeable_lines)
                order.has_returnable_lines = bool(returnable_lines)
            else:
                order.has_pickable_lines = False
                order.has_returnable_lines = False
                order.enroll_status = order.state if order.is_enrollment_order else False
                order.next_action_date = False

    # PICKUP / RETURN : rental.processing wizard

    def open_pickup(self):
        status = "pickup"
        precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')
        lines_to_pickup = self.order_line.filtered(
            lambda r: r.state in ['sale', 'done'] and r.is_enrollment and float_compare(r.product_uom_qty, r.qty_delivered, precision_digits=precision) > 0)
        return self._open_rental_wizard(status, lines_to_pickup.ids)

    def open_return(self):
        status = "return"
        precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')
        lines_to_return = self.order_line.filtered(
            lambda r: r.state in ['sale', 'done'] and r.is_enrollment and float_compare(r.qty_delivered, r.qty_returned, precision_digits=precision) > 0)
        return self._open_rental_wizard(status, lines_to_return.ids)

    def update_prices(self):
        super().update_prices()
        # Apply correct rental prices with respect to pricelist
        for sol in self.order_line.filtered(lambda line: line.is_enrollment):
            pricing = sol.product_id._get_best_pricing_rule(
                pickup_date=sol.pickup_date,
                return_date=sol.return_date,
                pricelist=self.pricelist_id,
                currency=self.currency_id,
                company=self.company_id
            )
            if not pricing:
                sol.price_unit = sol.product_id.lst_price
                continue
            duration_dict = self.env['rental.pricing']._compute_duration_vals(sol.pickup_date, sol.return_date)
            price = pricing._compute_price(duration_dict[pricing.unit], pricing.unit)

            if pricing.currency_id != self.currency_id:
                price = pricing.currency_id._convert(
                    from_amount=price,
                    to_currency=self.currency_id,
                    company=self.company_id,
                    date=date.today(),
                )
            sol.price_unit = price
            sol.discount = 0

    def _open_rental_wizard(self, status, order_line_ids):
        context = {
            'order_line_ids': order_line_ids,
            'default_status': status,
            'default_order_id': self.id,
        }
        return {
            'name': _('Validate a pickup') if status == 'pickup' else _('Validate a return'),
            'view_mode': 'form',
            'res_model': 'rental.order.wizard',
            'type': 'ir.actions.act_window',
            'target': 'new',
            'context': context
        }

    def _get_portal_return_action(self):
        """ Return the action used to display orders when returning from customer portal. """
        if self.is_enrollment_order:
            return self.env.ref('sale_renting.rental_order_action')
        else:
            return super(RentalOrder, self)._get_portal_return_action()


class EnrollmentOrderLine(models.Model):
    _inherit = 'sale.order.line'

    is_enrollment = fields.Boolean(default=False)  # change to compute if pickup_date and return_date set?

    qty_returned = fields.Float("Returned", default=0.0, copy=False)

    pickup_date = fields.Datetime(string="Pickup")
    return_date = fields.Datetime(string="Return")
    reservation_begin = fields.Datetime("Pickup date - padding time", compute='_compute_reservation_begin', store=True)

    is_late = fields.Boolean(string="Is overdue", compute="_compute_is_late", help="The products haven't been returned in time")

    is_product_rentable = fields.Boolean()
    rental_updatable = fields.Boolean(compute='_compute_rental_updatable')
    # TODO use is_product_rentable in rental_configurator_widget instead of rpc call?

    @api.depends('return_date')
    def _compute_is_late(self):
        now = fields.Datetime.now()
        for line in self:
            # By default, an order line is considered late only if it has one hour of delay
            line.is_late = line.return_date and line.return_date + timedelta(hours=self.company_id.min_extra_hour) < now

    @api.depends('pickup_date')
    def _compute_reservation_begin(self):
        lines = self.filtered(lambda line: line.is_enrollment)
        for line in lines:
            line.reservation_begin = line.pickup_date
        (self - lines).reservation_begin = None

    @api.depends('state', 'qty_invoiced', 'qty_delivered')
    def _compute_rental_updatable(self):
        rental_lines = self.filtered('is_enrollment')
        sale_lines = self - rental_lines
        for line in sale_lines:
            line.rental_updatable = line.product_updatable
        rental_lines.rental_updatable = True
        # for line in rental_lines:
        #     if line.state == 'cancel' or (line.state in ['sale', 'done'] and (line.qty_invoiced > 0 or line.qty_delivered > 0)):
        #         line.rental_updatable = False
        #     else:
        #         line.rental_updatable = True

    @api.onchange('product_id')
    def _onchange_product_id(self):
        """Clean rental related data if new product cannot be rented."""
        if (not self.is_product_rentable) and self.is_enrollment:
            self.update({
                'is_enrollment': False,
                'pickup_date': False,
                'return_date': False,
            })

    @api.onchange('qty_delivered')
    def _onchange_qty_delivered(self):
        """When picking up more than reserved, reserved qty is updated"""
        if self.qty_delivered > self.product_uom_qty:
            self.product_uom_qty = self.qty_delivered

    @api.onchange('pickup_date', 'return_date')
    def _onchange_rental_info(self):
        """Trigger description recomputation"""
        self._update_description()

    @api.onchange('is_enrollment')
    def _onchange_is_enrollment(self):
        if self.is_enrollment and not self.order_id.is_enrollment_order:
            self.order_id.is_enrollment_order = True

    @api.onchange('product_id', 'price_unit', 'product_uom', 'product_uom_qty', 'tax_id')
    def _onchange_discount(self):
        if not self.is_enrollment:
            super(EnrollmentOrderLine, self)._onchange_discount()

    _sql_constraints = [
        ('rental_stock_coherence',
            "CHECK(NOT is_enrollment OR qty_returned <= qty_delivered)",
            "You cannot return more than what has been picked up."),
        ('rental_period_coherence',
            "CHECK(NOT is_enrollment OR pickup_date < return_date)",
            "Please choose a return date that is after the pickup date."),
    ]

    def get_sale_order_line_multiline_description_sale(self, product):
        """Add Rental information to the SaleOrderLine name."""
        return super(EnrollmentOrderLine, self).get_sale_order_line_multiline_description_sale(product) + self.get_rental_order_line_description()

    def get_rental_order_line_description(self):
        if (self.is_enrollment):
            if self.pickup_date.replace(tzinfo=UTC).astimezone(timezone(self.env.user.tz or 'UTC')).replace(tzinfo=None).date()\
                 == self.return_date.replace(tzinfo=UTC).astimezone(timezone(self.env.user.tz or 'UTC')).replace(tzinfo=None).date():
                # If return day is the same as pickup day, don't display return_date Y/M/D in description.
                return_date_part = format_time(self.with_context(use_babel=True).env, self.return_date, tz=self.env.user.tz, time_format=False)
            else:
                return_date_part = format_datetime(self.with_context(use_babel=True).env, self.return_date, tz=self.env.user.tz, dt_format=False)

            return "\n%s %s %s" % (
                format_datetime(self.with_context(use_babel=True).env, self.pickup_date, tz=self.env.user.tz, dt_format=False),
                _("to"),
                return_date_part,
            )
        else:
            return ""

    @api.onchange('product_uom', 'product_uom_qty')
    def product_uom_change(self):
        if not self.is_enrollment:
            super().product_uom_change()

    def _get_display_price(self, product):
        """Ensure unit price isn't recomputed."""
        if self.is_enrollment:
            return self.price_unit
        else:
            return super(EnrollmentOrderLine, self)._get_display_price(product)

    def _generate_delay_line(self, qty):
        """Generate a sale order line representing the delay cost due to the late return.

        :param float qty:
        :param timedelta duration:
        """
        self.ensure_one()
        if qty <= 0 or not self.is_late:
            return

        duration = fields.Datetime.now() - self.return_date

        delay_price = self.product_id._compute_delay_price(duration)
        if delay_price <= 0.0:
            return

        # migrate to a function on res_company get_extra_product?
        delay_product = self.company_id.extra_product
        if not delay_product:
            delay_product = self.env['product.product'].with_context(active_test=False).search(
                [('default_code', '=', 'RENTAL'), ('type', '=', 'service')], limit=1)
            if not delay_product:
                delay_product = self.env['product.product'].create({
                    "name": "Rental Delay Cost",
                    "standard_price": 0.0,
                    "type": 'service',
                    "default_code": "RENTAL",
                    "purchase_ok": False,
                })
                # Not set to inactive to allow users to put it back in the settings
                # In case they removed it.
            self.company_id.extra_product = delay_product

        if not delay_product.active:
            return

        delay_price = self.product_id.currency_id._convert(
            from_amount=delay_price,
            to_currency=self.currency_id,
            company=self.company_id,
            date=date.today(),
        )

        vals = self._prepare_delay_line_vals(delay_product, delay_price, qty)

        self.order_id.write({
            'order_line': [(0, 0, vals)]
        })

    def _prepare_delay_line_vals(self, delay_product, delay_price, qty):
        """Prepare values of delay line.

        :param float delay_price:
        :param float quantity:
        :param delay_product: Product used for the delay_line
        :type delay_product: product.product
        :return: sale.order.line creation values
        :rtype dict:
        """
        delay_line_description = self._get_delay_line_description()
        return {
            'name': delay_line_description,
            'product_id': delay_product.id,
            'product_uom_qty': qty,
            'product_uom': self.product_id.uom_id.id,
            'qty_delivered': qty,
            'price_unit': delay_price,
        }

    def _get_delay_line_description(self):
        # Shouldn't tz be taken from self.order_id.user_id.tz ?
        return "%s\n%s: %s\n%s: %s" % (
            self.product_id.name,
            _("Expected"),
            format_datetime(self.with_context(use_babel=True).env, self.return_date, tz=self.env.user.tz, dt_format=False),
            _("Returned"),
            format_datetime(self.with_context(use_babel=True).env, fields.Datetime.now(), tz=self.env.user.tz, dt_format=False)
        )
