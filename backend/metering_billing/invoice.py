from __future__ import absolute_import

import datetime
from decimal import Decimal

import lotus_python
from django.conf import settings
from django.db.models import Sum
from djmoney.money import Money
from metering_billing.payment_providers import PAYMENT_PROVIDER_MAP
from metering_billing.utils import (
    calculate_end_date,
    convert_to_date,
    convert_to_datetime,
    convert_to_decimal,
    now_utc,
)
from metering_billing.utils.enums import (
    CUSTOMER_BALANCE_ADJUSTMENT_STATUS,
    FLAT_FEE_BILLING_TYPE,
    INVOICE_STATUS,
)
from metering_billing.webhooks import invoice_created_webhook

POSTHOG_PERSON = settings.POSTHOG_PERSON
META = settings.META
# LOTUS_HOST = settings.LOTUS_HOST
# LOTUS_API_KEY = settings.LOTUS_API_KEY
# if LOTUS_HOST and LOTUS_API_KEY:
#     lotus_python.api_key = LOTUS_API_KEY
#     lotus_python.host = LOTUS_HOST


def generate_invoice(
    subscription,
    draft=False,
    charge_next_plan=False,
    flat_fee_cutoff_date=None,
    include_usage=True,
    issue_date=None,
):
    """
    Generate an invoice for a subscription.
    """
    from metering_billing.models import (
        CustomerBalanceAdjustment,
        Invoice,
        InvoiceLineItem,
        PlanVersion,
    )
    from metering_billing.serializers.model_serializers import InvoiceSerializer

    assert (
        flat_fee_cutoff_date == -1
        or isinstance(flat_fee_cutoff_date, datetime.date)
        or flat_fee_cutoff_date is None
    ), "flat_fee_cutoff_date must be a date, None, or -1"

    if not issue_date:
        issue_date = now_utc()
    if isinstance(flat_fee_cutoff_date, datetime.date):
        flat_fee_cutoff_date = convert_to_date(flat_fee_cutoff_date)
    issue_date_fmt = issue_date.strftime("%Y-%m-%d")

    customer = subscription.customer
    organization = subscription.organization
    billing_plan = subscription.billing_plan
    pricing_unit = billing_plan.pricing_unit

    # create kwargs for invoice
    invoice_kwargs = {
        "issue_date": issue_date,
        "organization": organization,
        "customer": customer,
        "subscription": subscription,
        "payment_status": INVOICE_STATUS.DRAFT if draft else INVOICE_STATUS.UNPAID,
    }
    # Create the invoice
    invoice = Invoice.objects.create(**invoice_kwargs)

    # usage calculation
    if include_usage:
        for plan_component in billing_plan.plan_components.all():
            usg_rev = plan_component.calculate_total_revenue(subscription)
            subperiods = usg_rev["subperiods"]
            for subperiod in subperiods:
                ili = InvoiceLineItem.objects.create(
                    name=str(plan_component.billable_metric.billable_metric_name),
                    start_date=subperiod["start_date"],
                    end_date=subperiod["end_date"],
                    quantity=subperiod["usage_qty"],
                    subtotal=subperiod["revenue"],
                    billing_type=FLAT_FEE_BILLING_TYPE.IN_ARREARS,
                    invoice=invoice,
                    associated_plan_version=subscription.billing_plan,
                )
                if "unique_identifier" in subperiod:
                    ili.metadata = subperiod["unique_identifier"]
                    ili.save()
    # flat fee calculation for current plan
    if flat_fee_cutoff_date != -1:
        flat_costs_dict_list = sorted(
            list(subscription.prorated_flat_costs_dict.items()), key=lambda x: x[0]
        )
        date_range_costs = [
            (
                0,
                flat_costs_dict_list[0][1]["plan_version_id"],
                flat_costs_dict_list[0][0],
                flat_costs_dict_list[0][0],
            )
        ]
        for k, v in flat_costs_dict_list:
            last_elem_amount, last_elem_plan, last_elem_start, _ = date_range_costs[-1]
            k = convert_to_date(k)
            issue_dt = convert_to_date(issue_date)
            if flat_fee_cutoff_date and k > flat_fee_cutoff_date:
                # if we are further along or on the cutoff date then stop counting
                break
            if v["plan_version_id"] != last_elem_plan:
                date_range_costs.append((v["amount"], v["plan_version_id"], k, k))
            else:
                last_elem_amount += v["amount"]
                date_range_costs[-1] = (
                    last_elem_amount,
                    last_elem_plan,
                    last_elem_start,
                    k,
                )
        amt_invoiced = subscription.amount_already_invoiced()
        if (
            len(date_range_costs) == 1
            and abs(float(amt_invoiced) - date_range_costs[0][0]) < 0.01
        ):
            pass
        else:
            for amount, plan_version_id, start, end in date_range_costs:
                cur_bp = PlanVersion.objects.get(
                    organization=organization, version_id=plan_version_id
                )
                billing_plan_name = cur_bp.plan.plan_name
                billing_plan_version = cur_bp.version
                InvoiceLineItem.objects.create(
                    name=f"{billing_plan_name} v{billing_plan_version} Prorated Flat Fee",
                    start_date=convert_to_datetime(start, date_behavior="min"),
                    end_date=convert_to_datetime(end, date_behavior="max"),
                    quantity=None,
                    subtotal=amount,
                    billing_type=cur_bp.flat_fee_billing_type,
                    invoice=invoice,
                    associated_plan_version=cur_bp,
                )
            if amt_invoiced > 0:
                InvoiceLineItem.objects.create(
                    name=f"{subscription.subscription_id} Already Invoiced",
                    start_date=issue_date,
                    end_date=issue_date,
                    quantity=None,
                    subtotal=-amt_invoiced,
                    billing_type=FLAT_FEE_BILLING_TYPE.IN_ADVANCE,
                    invoice=invoice,
                )
    # next plan flat fee calculation
    if charge_next_plan:
        if billing_plan.transition_to:
            next_bp = billing_plan.transition_to.display_version
        elif billing_plan.replace_with:
            next_bp = billing_plan.replace_with
        else:
            next_bp = billing_plan
        if next_bp.flat_fee_billing_type == FLAT_FEE_BILLING_TYPE.IN_ADVANCE:
            InvoiceLineItem.objects.create(
                name=f"{next_bp.plan.plan_name} v{next_bp.version} Flat Fee - Next Period",
                start_date=subscription.end_date,
                end_date=calculate_end_date(
                    next_bp.plan.plan_duration, subscription.end_date
                ),
                quantity=None,
                subtotal=next_bp.flat_rate,
                billing_type=FLAT_FEE_BILLING_TYPE.IN_ADVANCE,
                invoice=invoice,
                associated_plan_version=next_bp,
            )

    for obj in (
        invoice.inv_line_items.filter(associated_plan_version__isnull=False)
        .values("associated_plan_version")
        .distinct()
    ):
        plan_version = PlanVersion.objects.get(pk=obj["associated_plan_version"])
        if plan_version.price_adjustment:
            plan_amount = (
                invoice.inv_line_items.filter(
                    associated_plan_version=plan_version
                ).aggregate(tot=Sum("subtotal"))["tot"]
                or 0
            )
            price_adj_name = str(plan_version.price_adjustment)
            new_amount_due = billing_plan.price_adjustment.apply(plan_amount)
            new_amount_due = max(new_amount_due, Decimal(0))
            difference = new_amount_due - plan_amount
            InvoiceLineItem.objects.create(
                name=f"{plan_version.plan.plan_name} v{plan_version.version} {price_adj_name}",
                start_date=issue_date,
                end_date=issue_date,
                quantity=None,
                subtotal=difference,
                billing_type=FLAT_FEE_BILLING_TYPE.IN_ARREARS,
                invoice=invoice,
                associated_plan_version=plan_version,
            )

    subtotal = invoice.inv_line_items.aggregate(tot=Sum("subtotal"))["tot"] or 0
    if subtotal < 0 and not draft:
        CustomerBalanceAdjustment.objects.create(
            organization=organization,
            customer=customer,
            amount=-subtotal,
            description=f"Balance increase from invoice {invoice.invoice_id} generated on {issue_date_fmt}",
            created=issue_date,
            effective_at=issue_date,
            status=CUSTOMER_BALANCE_ADJUSTMENT_STATUS.ACTIVE,
        )
        InvoiceLineItem.objects.create(
            name=f"{subscription.subscription_id} Customer Balance Adjustment",
            start_date=issue_date,
            end_date=issue_date,
            quantity=None,
            subtotal=-subtotal,
            billing_type=FLAT_FEE_BILLING_TYPE.IN_ARREARS,
            invoice=invoice,
        )
        subscription.flat_fee_already_billed += subtotal
        subscription.save()
    elif subtotal > 0:
        customer_balance = CustomerBalanceAdjustment.get_pricing_unit_balance(customer)
        balance_adjustment = min(subtotal, customer_balance)
        if balance_adjustment > 0 and not draft:
            leftover = CustomerBalanceAdjustment.draw_down_amount(
                customer,
                balance_adjustment,
                description=f"Balance decrease from invoice {invoice.invoice_id} generated on {issue_date_fmt}",
            )
            subscription.flat_fee_already_billed += balance_adjustment - leftover
            subscription.save()
            InvoiceLineItem.objects.create(
                name=f"{subscription.subscription_id} Customer Balance Adjustment",
                start_date=issue_date,
                end_date=issue_date,
                quantity=None,
                subtotal=-balance_adjustment + leftover,
                billing_type=FLAT_FEE_BILLING_TYPE.IN_ARREARS,
                invoice=invoice,
            )

    invoice.cost_due = invoice.inv_line_items.aggregate(tot=Sum("subtotal"))["tot"] or 0
    if abs(invoice.cost_due) < 0.01 and not draft:
        invoice.payment_status = INVOICE_STATUS.PAID
    invoice.save()

    if not draft:
        for pp in customer.integrations.keys():
            if pp in PAYMENT_PROVIDER_MAP and PAYMENT_PROVIDER_MAP[pp].working():
                pp_connector = PAYMENT_PROVIDER_MAP[pp]
                customer_conn = pp_connector.customer_connected(customer)
                org_conn = pp_connector.organization_connected(organization)
                if customer_conn and org_conn:
                    invoice.external_payment_obj_id = (
                        pp_connector.create_payment_object(invoice)
                    )
                    invoice.external_payment_obj_type = pp
                    invoice.save()
                    break
        # if META:
        # lotus_python.track_event(
        #     customer_id=organization.company_name + str(organization.pk),
        #     event_name='create_invoice',
        #     properties={
        #         'amount': float(invoice.cost_due.amount),
        #         'currency': str(invoice.cost_due.currency),
        #         'customer': customer.customer_id,
        #         'subscription': subscription.subscription_id,
        #         'external_type': invoice.external_payment_obj_type,
        #         },
        # )
        invoice_created_webhook(invoice, organization)

    return invoice
